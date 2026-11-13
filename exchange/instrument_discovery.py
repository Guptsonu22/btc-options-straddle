"""
BTC Options Instrument Discovery for Bybit Testnet.

Implements discovery, inspection, validation, and caching of live BTC options
using official NautilusTrader 1.231.0 representations and Bybit V5 specifications.

Zero trading orders are placed by this module.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nautilus_trader.adapters.bybit.config import BybitEnvironment, BybitProductType
from nautilus_trader.adapters.bybit.factories import (
    get_cached_bybit_http_client,
    get_cached_bybit_instrument_provider,
)
from nautilus_trader.core.nautilus_pyo3.bybit import BybitHttpClient
from nautilus_trader.model.enums import MarketStatusAction, OptionKind
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments.crypto_option import CryptoOption

from config.settings import SecurityError, Settings, get_settings
from utils.logger import get_logger, setup_logger


class InstrumentDiscoveryError(Exception):
    """Raised when instrument discovery fails critically."""
    pass


class InvalidMetadataError(Exception):
    """Raised when an instrument fails contract specification validation."""
    pass


@dataclass(frozen=True)
class OptionContractDetails:
    """
    Standardized, validated representation of a single BTC Option contract.
    Wraps and validates NautilusTrader's CryptoOption model.
    """
    underlying: str
    instrument_id: str
    raw_symbol: str
    option_type: str  # "CALL" or "PUT"
    strike_price: float
    expiration_utc: datetime
    contract_multiplier: float
    settlement_currency: str
    min_order_quantity: float
    quantity_increment: float
    price_precision: int
    price_increment: float
    trading_status: str  # e.g. "TRADING", "HALT", "NOT_AVAILABLE_FOR_TRADING"
    is_active: bool
    nautilus_instrument: Optional[CryptoOption] = field(default=None, repr=False, hash=False, compare=False)

    @property
    def is_call(self) -> bool:
        return self.option_type == "CALL"

    @property
    def is_put(self) -> bool:
        return self.option_type == "PUT"

    def time_to_expiry_days(self, as_of: Optional[datetime] = None) -> float:
        """Calculate time until expiration in fractional days."""
        now = as_of or datetime.now(timezone.utc)
        delta = self.expiration_utc - now
        return max(0.0, delta.total_seconds() / 86400.0)

    def is_expired(self, as_of: Optional[datetime] = None) -> bool:
        """Check if contract is past its expiration."""
        now = as_of or datetime.now(timezone.utc)
        return self.expiration_utc <= now

    def to_dict(self) -> dict[str, Any]:
        """Export dictionary representation excluding complex Cython references."""
        data = asdict(self)
        data.pop("nautilus_instrument", None)
        data["expiration_utc"] = self.expiration_utc.isoformat()
        return data


@dataclass
class DiscoveredOptionsChain:
    """
    Indexed collection of discovered, validated BTC options for a given snapshot in time.
    """
    underlying: str
    as_of_utc: datetime
    total_discovered: int
    total_active: int
    calls: list[OptionContractDetails] = field(default_factory=list)
    puts: list[OptionContractDetails] = field(default_factory=list)
    expirations: list[datetime] = field(default_factory=list)
    strikes_by_expiry: dict[datetime, list[float]] = field(default_factory=dict)
    contracts_by_id: dict[str, OptionContractDetails] = field(default_factory=dict)
    _calls_map: dict[tuple[datetime, float], OptionContractDetails] = field(default_factory=dict)
    _puts_map: dict[tuple[datetime, float], OptionContractDetails] = field(default_factory=dict)

    def get_straddle_pair(
        self,
        expiration: datetime,
        strike: float,
    ) -> tuple[Optional[OptionContractDetails], Optional[OptionContractDetails]]:
        """
        Retrieve matched (Call, Put) pair for an exact expiration and strike.
        """
        call = self._calls_map.get((expiration, strike))
        put = self._puts_map.get((expiration, strike))
        return call, put

    def find_nearest_atm_strike(
        self,
        spot_price: float,
        expiration: datetime,
    ) -> Optional[float]:
        """
        Select strike K* minimizing absolute moneyness: argmin |K - S|.
        """
        strikes = self.strikes_by_expiry.get(expiration)
        if not strikes:
            return None
        return min(strikes, key=lambda k: abs(k - spot_price))

    def summary_table(self, max_rows: int = 15) -> str:
        """Generate human-readable summary of available expiration dates and strikes."""
        lines = [
            f"Underlying: {self.underlying} | Snapshot: {self.as_of_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"Total Discovered: {self.total_discovered} | Active (Unexpired): {self.total_active}",
            f"Calls: {len(self.calls)} | Puts: {len(self.puts)} | Unique Expirations: {len(self.expirations)}",
            "-" * 72,
            f"{'Expiration (UTC)':<22} | {'Strikes Count':<14} | {'Strike Range (USD)':<24} | {'Pairs'}",
            "-" * 72,
        ]
        displayed = self.expirations[:max_rows]
        for exp in displayed:
            strikes = self.strikes_by_expiry.get(exp, [])
            min_k = min(strikes) if strikes else 0.0
            max_k = max(strikes) if strikes else 0.0
            # Count complete straddle pairs
            pairs = sum(1 for k in strikes if (exp, k) in self._calls_map and (exp, k) in self._puts_map)
            exp_str = exp.strftime("%Y-%m-%d %H:%M")
            lines.append(f"{exp_str:<22} | {len(strikes):<14} | ${min_k:,.0f} - ${max_k:,.0f} | {pairs}")

        if len(self.expirations) > max_rows:
            lines.append(f"... and {len(self.expirations) - max_rows} more expiration dates.")
        lines.append("-" * 72)
        return "\n".join(lines)


class BtcOptionsDiscoveryService:
    """
    Service responsible for querying Bybit Testnet options, parsing NautilusTrader
    CryptoOption objects, validating contract metadata, and managing in-memory caching.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.logger = get_logger("btc_straddle.discovery")
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: Optional[DiscoveredOptionsChain] = None
        self._cache_timestamp: float = 0.0

        if not self.settings.BYBIT_TESTNET:
            raise SecurityError("BtcOptionsDiscoveryService aborted: BYBIT_TESTNET must be True.")

    def is_cache_valid(self) -> bool:
        """Check if cached options chain is still within TTL."""
        if self._cache is None:
            return False
        return (time.time() - self._cache_timestamp) < self.cache_ttl_seconds

    def clear_cache(self) -> None:
        """Invalidate the local discovery cache."""
        self._cache = None
        self._cache_timestamp = 0.0
        self.logger.debug("Discovery cache cleared.")

    @staticmethod
    def validate_metadata(
        instrument: CryptoOption,
        underlying: str = "BTC",
    ) -> tuple[bool, Optional[str]]:
        """
        Validate contract metadata against Bybit Options specifications.

        Validation Rules:
        1. Underlying matches requested coin (BTC).
        2. Strike price > 0.
        3. Expiration timestamp is valid and non-zero.
        4. Multiplier is positive (Bybit BTC options multiplier is 1.0).
        5. Settlement currency is USDT or USDC.
        6. Minimum order quantity > 0 (Bybit is 0.01).
        7. Quantity increment > 0 (Bybit is 0.01).
        8. Price precision >= 0.
        """
        # 1. Underlying coin
        inst_underlying = getattr(instrument, "underlying", None)
        inst_underlying_str = str(inst_underlying) if inst_underlying else ""
        if inst_underlying_str.upper() != underlying.upper():
            return False, f"Underlying mismatch: expected {underlying}, got {inst_underlying_str}"

        # 2. Strike price
        strike = float(instrument.strike_price) if hasattr(instrument, "strike_price") else 0.0
        if strike <= 0.0:
            return False, f"Invalid strike price: {strike}"

        # 3. Expiration
        if not hasattr(instrument, "expiration_ns") or instrument.expiration_ns <= 0:
            return False, f"Missing or invalid expiration_ns: {getattr(instrument, 'expiration_ns', None)}"

        # 4. Multiplier
        multiplier = float(instrument.multiplier) if hasattr(instrument, "multiplier") else 0.0
        if multiplier <= 0.0:
            return False, f"Invalid contract multiplier: {multiplier}"

        # 5. Settlement currency
        settle = str(instrument.settlement_currency) if hasattr(instrument, "settlement_currency") else ""
        if settle.upper() not in ("USDT", "USDC", "USD"):
            return False, f"Unexpected settlement currency: {settle}"

        # 6. Min quantity
        min_qty = float(instrument.min_quantity) if hasattr(instrument, "min_quantity") else 0.0
        if min_qty <= 0.0:
            return False, f"Invalid min_quantity: {min_qty}"

        # 7. Size increment
        step = float(instrument.size_increment) if hasattr(instrument, "size_increment") else 0.0
        if step <= 0.0:
            return False, f"Invalid size_increment: {step}"

        # 8. Price precision
        precision = int(instrument.price_precision) if hasattr(instrument, "price_precision") else -1
        if precision < 0:
            return False, f"Invalid price_precision: {precision}"

        return True, None

    def process_instruments(
        self,
        instruments: list[CryptoOption],
        statuses: Optional[dict[InstrumentId, MarketStatusAction]] = None,
        now_utc: Optional[datetime] = None,
        active_only: bool = True,
        min_time_to_expiry_hours: float = 0.0,
    ) -> DiscoveredOptionsChain:
        """
        Transform raw NautilusTrader CryptoOption objects into a validated DiscoveredOptionsChain.

        Parameters
        ----------
        instruments : list[CryptoOption]
            List of CryptoOption instances from Bybit.
        statuses : Optional[dict[InstrumentId, MarketStatusAction]]
            Dictionary of live market trading statuses.
        now_utc : Optional[datetime]
            Timestamp to anchor expiration checks against.
        active_only : bool
            Whether to exclude expired contracts and contracts not currently in TRADING status.
        min_time_to_expiry_hours : float
            Minimum hours required before expiration.

        Returns
        -------
        DiscoveredOptionsChain
            The fully parsed, indexed options chain.
        """
        now = now_utc or datetime.now(timezone.utc)
        statuses = statuses or {}

        calls: list[OptionContractDetails] = []
        puts: list[OptionContractDetails] = []
        contracts_by_id: dict[str, OptionContractDetails] = []
        calls_map: dict[tuple[datetime, float], OptionContractDetails] = {}
        puts_map: dict[tuple[datetime, float], OptionContractDetails] = {}
        strikes_by_expiry: dict[datetime, set[float]] = {}
        all_contracts_by_id: dict[str, OptionContractDetails] = {}

        total_discovered = len(instruments)
        valid_count = 0

        for inst in instruments:
            # 1. Metadata Validation
            is_valid, error_msg = self.validate_metadata(inst, underlying=self.settings.BTC_SYMBOL)
            if not is_valid:
                self.logger.warning(f"Skipping instrument {getattr(inst, 'id', 'UNKNOWN')}: {error_msg}")
                continue

            # 2. Extract Expiration
            exp_ts = inst.expiration_utc
            if hasattr(exp_ts, "to_pydatetime"):
                exp_dt = exp_ts.to_pydatetime()
            elif isinstance(exp_ts, datetime):
                exp_dt = exp_ts
            else:
                # Convert nanoseconds to UTC datetime
                exp_dt = datetime.fromtimestamp(inst.expiration_ns / 1e9, tz=timezone.utc)

            # Ensure UTC timezone awareness
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)

            # 3. Filter Expired Contracts
            time_to_expiry_hrs = (exp_dt - now).total_seconds() / 3600.0
            is_expired = time_to_expiry_hrs <= min_time_to_expiry_hours

            # 4. Trading Status
            raw_status = statuses.get(inst.id)
            if raw_status is not None:
                if hasattr(raw_status, "name"):
                    status_str = raw_status.name
                else:
                    status_str = str(raw_status).split(".")[-1]
                is_trading = (status_str.upper() == "TRADING" or raw_status == MarketStatusAction.TRADING)
            else:
                status_str = "TRADING"  # Default assumption for public feeds if status endpoint unavailable
                is_trading = True

            is_active = is_trading and (not is_expired)

            if active_only and not is_active:
                continue

            # 5. Extract Option Type
            kind = inst.option_kind
            is_call = (kind == OptionKind.CALL or str(kind).endswith("CALL") or kind == 1)
            option_type = "CALL" if is_call else "PUT"

            # 6. Build Contract Details
            strike = float(inst.strike_price)
            details = OptionContractDetails(
                underlying=str(inst.underlying),
                instrument_id=str(inst.id),
                raw_symbol=str(inst.raw_symbol),
                option_type=option_type,
                strike_price=strike,
                expiration_utc=exp_dt,
                contract_multiplier=float(inst.multiplier),
                settlement_currency=str(inst.settlement_currency),
                min_order_quantity=float(inst.min_quantity),
                quantity_increment=float(inst.size_increment),
                price_precision=int(inst.price_precision),
                price_increment=float(inst.price_increment),
                trading_status=status_str,
                is_active=is_active,
                nautilus_instrument=inst,
            )

            all_contracts_by_id[details.instrument_id] = details

            if is_call:
                calls.append(details)
                calls_map[(exp_dt, strike)] = details
            else:
                puts.append(details)
                puts_map[(exp_dt, strike)] = details

            if exp_dt not in strikes_by_expiry:
                strikes_by_expiry[exp_dt] = set()
            strikes_by_expiry[exp_dt].add(strike)
            valid_count += 1

        # Sort expirations chronologically
        sorted_expirations = sorted(strikes_by_expiry.keys())

        # Sort strikes ascending for each expiry
        sorted_strikes_by_expiry = {
            exp: sorted(list(strikes_by_expiry[exp])) for exp in sorted_expirations
        }

        # Sort contract lists by (expiration, strike)
        calls.sort(key=lambda c: (c.expiration_utc, c.strike_price))
        puts.sort(key=lambda p: (p.expiration_utc, p.strike_price))

        chain = DiscoveredOptionsChain(
            underlying=self.settings.BTC_SYMBOL,
            as_of_utc=now,
            total_discovered=total_discovered,
            total_active=valid_count,
            calls=calls,
            puts=puts,
            expirations=sorted_expirations,
            strikes_by_expiry=sorted_strikes_by_expiry,
            contracts_by_id=all_contracts_by_id,
            _calls_map=calls_map,
            _puts_map=puts_map,
        )

        return chain

    async def discover_btc_options(
        self,
        active_only: bool = True,
        force_refresh: bool = False,
        min_time_to_expiry_hours: float = 0.0,
    ) -> DiscoveredOptionsChain:
        """
        Query Bybit Testnet via NautilusTrader HTTP client & instrument provider.

        Parameters
        ----------
        active_only : bool
            Filter out expired contracts or contracts not in TRADING status.
        force_refresh : bool
            Bypass cached chain and perform a fresh network query.
        min_time_to_expiry_hours : float
            Only include contracts with at least this many hours until expiration.

        Returns
        -------
        DiscoveredOptionsChain
            Parsed options chain.
        """
        if not force_refresh and self.is_cache_valid():
            assert self._cache is not None
            self.logger.info(
                f"Returning cached BTC options chain ({self._cache.total_active} active contracts)."
            )
            return self._cache

        self.logger.info("Connecting to Bybit Testnet to discover BTC options chain...")
        start_time = time.monotonic()

        api_key = (
            self.settings.BYBIT_API_KEY.get_secret_value()
            if self.settings.BYBIT_API_KEY
            else None
        )
        api_secret = (
            self.settings.BYBIT_API_SECRET.get_secret_value()
            if self.settings.BYBIT_API_SECRET
            else None
        )
        has_credentials = bool(api_key and api_secret and "your_bybit" not in api_key.lower())

        try:
            # 1. Initialize official cached Bybit HTTP client
            http_client: BybitHttpClient = get_cached_bybit_http_client(
                environment=BybitEnvironment.TESTNET,
                api_key=api_key if has_credentials else None,
                api_secret=api_secret if has_credentials else None,
            )

            # 2. Query instrument provider
            provider = get_cached_bybit_instrument_provider(
                client=http_client,
                product_types=(BybitProductType.OPTION,),
            )

            self.logger.info("Requesting all BTC options instruments from Bybit...")
            await provider.load_all_async(filters={"base_coin": self.settings.BTC_SYMBOL})
            instruments = provider.list_all()

            # 3. Query real-time instrument statuses
            statuses: dict[InstrumentId, MarketStatusAction] = {}
            try:
                statuses = await http_client.request_instrument_statuses(BybitProductType.OPTION)
            except Exception as e:
                self.logger.warning(f"Could not retrieve instrument statuses (continuing with defaults): {e}")

            elapsed = round((time.monotonic() - start_time) * 1000, 2)
            self.logger.info(
                f"Retrieved {len(instruments)} raw options from Bybit in {elapsed}ms."
            )

            # 4. Handle Empty Results
            if not instruments:
                self.logger.warning("No BTC options returned by Bybit Testnet.")
                chain = DiscoveredOptionsChain(
                    underlying=self.settings.BTC_SYMBOL,
                    as_of_utc=datetime.now(timezone.utc),
                    total_discovered=0,
                    total_active=0,
                )
                return chain

            # 5. Process, Validate, and Structure
            chain = self.process_instruments(
                instruments=instruments,
                statuses=statuses,
                active_only=active_only,
                min_time_to_expiry_hours=min_time_to_expiry_hours,
            )

            # 6. Update Cache
            self._cache = chain
            self._cache_timestamp = time.time()

            self.logger.info(
                f"Options Discovery complete: {chain.total_active} active contracts "
                f"({len(chain.calls)} Calls, {len(chain.puts)} Puts across {len(chain.expirations)} expirations)."
            )

            return chain

        except Exception as e:
            self.logger.error(f"Failed to discover BTC options: {e}")
            raise InstrumentDiscoveryError(f"Options discovery failed: {e}") from e


async def run_discovery_cli() -> int:
    """Entry point for standalone CLI execution."""
    logger = setup_logger("btc_straddle.discovery")
    logger.info("=" * 72)
    logger.info("BYBIT TESTNET - BTC OPTIONS INSTRUMENT DISCOVERY (PART 5)")
    logger.info("=" * 72)

    service = BtcOptionsDiscoveryService()
    try:
        chain = await service.discover_btc_options(active_only=True)
        print("\n" + chain.summary_table(max_rows=15) + "\n")

        # Display sample Call and Put details
        if chain.calls:
            c0 = chain.calls[0]
            logger.info("Sample Active Call Option:")
            logger.info(f"  ID: {c0.instrument_id}")
            logger.info(f"  Strike: ${c0.strike_price:,.0f} | Expiry: {c0.expiration_utc.strftime('%Y-%m-%d %H:%M UTC')}")
            logger.info(f"  Min Qty: {c0.min_order_quantity} BTC | Step: {c0.quantity_increment} BTC | Mult: {c0.contract_multiplier}")
            logger.info(f"  Price Precision: {c0.price_precision} | Settle: {c0.settlement_currency} | Status: {c0.trading_status}")

        if chain.puts:
            p0 = chain.puts[0]
            logger.info("Sample Active Put Option:")
            logger.info(f"  ID: {p0.instrument_id}")
            logger.info(f"  Strike: ${p0.strike_price:,.0f} | Expiry: {p0.expiration_utc.strftime('%Y-%m-%d %H:%M UTC')}")
            logger.info(f"  Min Qty: {p0.min_order_quantity} BTC | Step: {p0.quantity_increment} BTC | Mult: {p0.contract_multiplier}")
            logger.info(f"  Price Precision: {p0.price_precision} | Settle: {p0.settlement_currency} | Status: {p0.trading_status}")

        logger.info("=" * 72)
        logger.info("[SUCCESS] BTC Options discovery verified with live Testnet data.")
        logger.info("=" * 72)
        return 0
    except Exception as e:
        logger.error(f"Discovery error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_discovery_cli()))
