"""
At-The-Money (ATM) Strike and Option Instrument Selector.

Implements ATM strike selection formula:
    K* = argmin_{K in K} |K - S|
with deterministic tie-breaking, configurable expiration selection policies,
strict Call/Put pair matching, and instrument status validation.

Zero trading orders are placed by this module.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import Settings, get_settings
from exchange.instrument_discovery import (
    BtcOptionsDiscoveryService,
    DiscoveredOptionsChain,
    OptionContractDetails,
)
from exchange.market_data import MarketDataManager
from utils.logger import get_logger, setup_logger


class ExpirySelectionPolicy(str, Enum):
    """Supported policies for selecting the target options expiration date."""
    NEAREST = "NEAREST"            # Earliest valid unexpired expiry with DTE >= min_dte_hours
    TARGET_DTE = "TARGET_DTE"      # Closest expiry to a specific target days-to-expiry (e.g. 7 or 14 days)
    EXPLICIT_DATE = "EXPLICIT_DATE"# Specific requested expiration date


class TieBreakerPolicy(str, Enum):
    """Deterministic policy when two strikes are equidistant from the reference price."""
    LOWER = "LOWER"  # Choose min(K1, K2)
    UPPER = "UPPER"  # Choose max(K1, K2)


class SelectionError(Exception):
    """Base exception for instrument selection failures."""
    pass


class NoActiveInstrumentsError(SelectionError):
    """Raised when the options chain contains no valid or unexpired instruments."""
    pass


class NoValidExpiryError(SelectionError):
    """Raised when no expiration dates meet the policy or minimum time-to-expiry threshold."""
    pass


class IncompletePairError(SelectionError):
    """Raised when a selected strike lacks a matching Call or Put contract."""
    pass


class MismatchedPairError(SelectionError):
    """Raised when a pair fails strike or expiration equality validation."""
    pass


@dataclass(frozen=True)
class SelectedStraddleLegs:
    """
    Verified and validated pair of Call and Put options for ATM straddle execution.
    """
    underlying: str
    underlying_price: float
    expiration_utc: datetime
    strike_price: float
    call: OptionContractDetails
    put: OptionContractDetails
    moneyness_abs: float
    moneyness_pct: float
    time_to_expiry_days: float
    time_to_expiry_hours: float

    def format_summary(self) -> str:
        """Generate human-readable diagnostic display."""
        lines = [
            "=" * 72,
            "SELECTED ATM STRADDLE PAIR SPECIFICATION",
            "=" * 72,
            f"Underlying Asset    : {self.underlying}",
            f"Reference Spot Price: ${self.underlying_price:,.2f}",
            f"Target Expiration   : {self.expiration_utc.strftime('%Y-%m-%d %H:%M UTC')}",
            f"Time to Expiry      : {self.time_to_expiry_days:.2f} days ({self.time_to_expiry_hours:.1f} hours)",
            f"Selected ATM Strike : ${self.strike_price:,.0f}",
            f"Moneyness Offset    : ${self.moneyness_abs:,.2f} ({self.moneyness_pct:.3f}%)",
            "-" * 72,
            "CALL LEG DETAILS:",
            f"  Instrument ID     : {self.call.instrument_id}",
            f"  Raw Symbol        : {self.call.raw_symbol}",
            f"  Strike Price      : ${self.call.strike_price:,.0f}",
            f"  Option Type       : {self.call.option_type}",
            f"  Multiplier        : {self.call.contract_multiplier}",
            f"  Min Quantity      : {self.call.min_order_quantity} BTC (step: {self.call.quantity_increment})",
            f"  Status            : {self.call.trading_status} (active={self.call.is_active})",
            "-" * 72,
            "PUT LEG DETAILS:",
            f"  Instrument ID     : {self.put.instrument_id}",
            f"  Raw Symbol        : {self.put.raw_symbol}",
            f"  Strike Price      : ${self.put.strike_price:,.0f}",
            f"  Option Type       : {self.put.option_type}",
            f"  Multiplier        : {self.put.contract_multiplier}",
            f"  Min Quantity      : {self.put.min_order_quantity} BTC (step: {self.put.quantity_increment})",
            f"  Status            : {self.put.trading_status} (active={self.put.is_active})",
            "=" * 72,
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        data = asdict(self)
        data["expiration_utc"] = self.expiration_utc.isoformat()
        data["call"] = self.call.to_dict()
        data["put"] = self.put.to_dict()
        return data


class InstrumentSelector:
    """
    Core engine for ATM strike selection, expiry filtering, and Call/Put pair reconciliation.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        expiry_policy: ExpirySelectionPolicy = ExpirySelectionPolicy.NEAREST,
        min_dte_hours: float = 2.0,
        target_dte_days: Optional[float] = None,
        explicit_expiry: Optional[datetime | str] = None,
        tie_breaker: TieBreakerPolicy = TieBreakerPolicy.LOWER,
        allow_fallback_to_next_nearest: bool = True,
        require_quotes: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.logger = get_logger("btc_straddle.selector")
        self.expiry_policy = expiry_policy
        self.min_dte_hours = min_dte_hours
        self.target_dte_days = target_dte_days
        self.explicit_expiry = explicit_expiry
        self.tie_breaker = tie_breaker
        self.allow_fallback_to_next_nearest = allow_fallback_to_next_nearest
        self.require_quotes = require_quotes

    def select_expiry(
        self,
        chain: DiscoveredOptionsChain,
        as_of_utc: Optional[datetime] = None,
    ) -> datetime:
        """
        Select an expiration date from the options chain matching the configured policy.

        Filters:
        1. Expirations must be in the future.
        2. Time until expiration must be >= min_dte_hours.
        """
        now = as_of_utc or datetime.now(timezone.utc)

        if not chain.expirations:
            raise NoActiveInstrumentsError("Options chain contains zero expirations.")

        # Filter out expiries too close to expiration
        valid_expiries = [
            exp for exp in chain.expirations
            if (exp - now).total_seconds() >= (self.min_dte_hours * 3600.0)
        ]

        if not valid_expiries:
            raise NoValidExpiryError(
                f"No expirations meet the minimum threshold of {self.min_dte_hours} hours. "
                f"Total available: {len(chain.expirations)}"
            )

        # Policy 1: Explicit Date
        if self.expiry_policy == ExpirySelectionPolicy.EXPLICIT_DATE:
            if not self.explicit_expiry:
                raise SelectionError("EXPLICIT_DATE policy selected but no explicit_expiry provided.")

            target_str = (
                self.explicit_expiry.strftime("%Y-%m-%d")
                if isinstance(self.explicit_expiry, datetime)
                else str(self.explicit_expiry)
            )

            for exp in valid_expiries:
                if target_str in exp.strftime("%Y-%m-%d") or target_str.upper() in exp.strftime("%d%b%y").upper():
                    return exp
            raise NoValidExpiryError(f"Explicit expiration date '{target_str}' not found in active chain.")

        # Policy 2: Target DTE (Days to Expiration)
        elif self.expiry_policy == ExpirySelectionPolicy.TARGET_DTE:
            if self.target_dte_days is None:
                raise SelectionError("TARGET_DTE policy selected but no target_dte_days provided.")

            target_sec = self.target_dte_days * 86400.0
            best_exp = min(valid_expiries, key=lambda exp: abs((exp - now).total_seconds() - target_sec))
            return best_exp

        # Policy 3: Nearest valid expiry (default)
        else:
            return valid_expiries[0]

    def select_atm_strike(
        self,
        strikes: list[float],
        spot_price: float,
    ) -> float:
        """
        Find the strike minimizing absolute distance to the reference price:
            K* = argmin_{K in strikes} |K - S|

        Deterministic Tie-Breaking:
        If two strikes K1 and K2 have identical distance:
        - LOWER: choose min(K1, K2)
        - UPPER: choose max(K1, K2)
        """
        if not strikes:
            raise SelectionError("Cannot select ATM strike from an empty strikes list.")

        # Sort strikes ascending first to ensure deterministic ordering
        sorted_strikes = sorted(strikes)

        # Find minimum distance
        min_dist = min(abs(k - spot_price) for k in sorted_strikes)

        # Collect all strikes achieving this minimum distance
        candidates = [k for k in sorted_strikes if abs(abs(k - spot_price) - min_dist) < 1e-6]

        if len(candidates) == 1:
            return candidates[0]

        # Apply tie-breaker
        if self.tie_breaker == TieBreakerPolicy.LOWER:
            selected = min(candidates)
            self.logger.info(
                f"Equidistant strike tie between {candidates} resolved via LOWER policy -> {selected}"
            )
            return selected
        else:
            selected = max(candidates)
            self.logger.info(
                f"Equidistant strike tie between {candidates} resolved via UPPER policy -> {selected}"
            )
            return selected

    def select_straddle_pair(
        self,
        chain: DiscoveredOptionsChain,
        spot_price: float,
        market_data_manager: Optional[MarketDataManager] = None,
        as_of_utc: Optional[datetime] = None,
    ) -> SelectedStraddleLegs:
        """
        Execute full straddle pair selection and verification pipeline.

        1. Select valid future expiration.
        2. Identify ATM strike candidate(s).
        3. Match Call and Put legs.
        4. Validate identical strike and expiry.
        5. Validate active status and quote availability.
        6. Return validated SelectedStraddleLegs.
        """
        now = as_of_utc or datetime.now(timezone.utc)

        if spot_price <= 0.0:
            raise SelectionError(f"Invalid underlying reference spot price: {spot_price}")

        if chain.total_active == 0:
            raise NoActiveInstrumentsError("Options chain has zero active instruments.")

        # 1. Select Expiration
        target_exp = self.select_expiry(chain, as_of_utc=now)
        strikes = chain.strikes_by_expiry.get(target_exp, [])

        if not strikes:
            raise NoActiveInstrumentsError(f"No strikes found for expiration {target_exp}.")

        # 2. Sort candidate strikes by distance from spot_price, applying tie-breaking
        def strike_sort_key(k: float) -> tuple[float, float]:
            dist = abs(k - spot_price)
            # Secondary tie-breaker sort key: negative if UPPER, positive if LOWER
            tie_val = -k if self.tie_breaker == TieBreakerPolicy.UPPER else k
            return (dist, tie_val)

        candidate_strikes = sorted(strikes, key=strike_sort_key)

        # 3. Find the first candidate strike with a complete, valid Call and Put pair
        selected_strike: Optional[float] = None
        selected_call: Optional[OptionContractDetails] = None
        selected_put: Optional[OptionContractDetails] = None

        for idx, k in enumerate(candidate_strikes):
            call_candidate, put_candidate = chain.get_straddle_pair(target_exp, k)

            is_complete = bool(call_candidate and put_candidate)

            if not is_complete:
                if not self.allow_fallback_to_next_nearest and idx == 0:
                    missing = "Call" if not call_candidate else "Put"
                    raise IncompletePairError(
                        f"Absolute nearest ATM strike ${k:,.0f} lacks a matching {missing} contract."
                    )
                continue

            assert call_candidate is not None
            assert put_candidate is not None

            # Validate active status
            if not call_candidate.is_active or not put_candidate.is_active:
                if not self.allow_fallback_to_next_nearest and idx == 0:
                    raise SelectionError(f"ATM strike ${k:,.0f} has inactive instruments.")
                continue

            # Validate quote availability if requested
            if self.require_quotes and market_data_manager:
                c_q = market_data_manager.get_quote(call_candidate.instrument_id)
                p_q = market_data_manager.get_quote(put_candidate.instrument_id)
                if not c_q or not p_q or not c_q.is_valid or not p_q.is_valid:
                    if not self.allow_fallback_to_next_nearest and idx == 0:
                        raise SelectionError(f"ATM strike ${k:,.0f} lacks active valid quotes.")
                    continue

            # Found complete valid pair
            selected_strike = k
            selected_call = call_candidate
            selected_put = put_candidate
            break

        if not selected_strike or not selected_call or not selected_put:
            raise IncompletePairError(
                f"Failed to find any complete, active Call/Put straddle pair for expiration {target_exp}."
            )

        # 4. Strict Contract Cross-Reconciliation Validations
        if selected_call.strike_price != selected_put.strike_price:
            raise MismatchedPairError(
                f"Strike mismatch: Call strike={selected_call.strike_price} vs Put strike={selected_put.strike_price}"
            )

        if selected_call.strike_price != selected_strike:
            raise MismatchedPairError(
                f"Selected strike mismatch: expected {selected_strike}, got {selected_call.strike_price}"
            )

        if selected_call.expiration_utc != selected_put.expiration_utc:
            raise MismatchedPairError(
                f"Expiration mismatch: Call exp={selected_call.expiration_utc} vs Put exp={selected_put.expiration_utc}"
            )

        if selected_call.option_type != "CALL" or selected_put.option_type != "PUT":
            raise MismatchedPairError("Option types invalid (expected 1 CALL and 1 PUT).")

        # 5. Calculate Metrics
        moneyness_abs = abs(selected_strike - spot_price)
        moneyness_pct = (moneyness_abs / spot_price) * 100.0
        time_to_exp_sec = (target_exp - now).total_seconds()
        dte_days = max(0.0, time_to_exp_sec / 86400.0)
        dte_hours = max(0.0, time_to_exp_sec / 3600.0)

        result = SelectedStraddleLegs(
            underlying=chain.underlying,
            underlying_price=spot_price,
            expiration_utc=target_exp,
            strike_price=selected_strike,
            call=selected_call,
            put=selected_put,
            moneyness_abs=round(moneyness_abs, 2),
            moneyness_pct=round(moneyness_pct, 4),
            time_to_expiry_days=round(dte_days, 3),
            time_to_expiry_hours=round(dte_hours, 2),
        )

        self.logger.info(
            f"ATM Straddle Pair Selected: Expiry={target_exp.strftime('%Y-%m-%d')} | "
            f"Strike=${selected_strike:,.0f} | Moneyness=${moneyness_abs:,.2f} ({moneyness_pct:.2f}%)"
        )

        return result


async def run_straddle_selection_cli() -> int:
    """CLI dispatcher to select and display the ATM straddle pair using live Bybit data."""
    logger = setup_logger("btc_straddle.selector")
    logger.info("=" * 72)
    logger.info("BYBIT TESTNET - ATM STRIKE & STRADDLE SELECTION (PART 7)")
    logger.info("=" * 72)

    settings = get_settings()

    # 1. Fetch live BTC spot / perpetual price
    from nautilus_trader.adapters.bybit.config import BybitEnvironment, BybitProductType
    from nautilus_trader.adapters.bybit.factories import get_cached_bybit_http_client
    from nautilus_trader.core.nautilus_pyo3.bybit import BybitTickersParams

    client = get_cached_bybit_http_client(environment=BybitEnvironment.TESTNET)
    tickers = await client.request_tickers(BybitTickersParams(category=BybitProductType.LINEAR, symbol="BTCUSDT"))

    if not tickers:
        logger.error("Failed to retrieve live BTCUSDT perpetual reference price.")
        return 1

    t0 = tickers[0]
    spot_price = float(t0.last_price or t0.bid1_price or 0.0)
    logger.info(f"Retrieved Live BTC Reference Price: ${spot_price:,.2f}")

    # 2. Discover options chain
    discovery = BtcOptionsDiscoveryService(settings=settings)
    chain = await discovery.discover_btc_options(active_only=True)

    # 3. Execute selector
    selector = InstrumentSelector(
        settings=settings,
        expiry_policy=ExpirySelectionPolicy.NEAREST,
        min_dte_hours=2.0,
        tie_breaker=TieBreakerPolicy.LOWER,
    )

    try:
        selected = selector.select_straddle_pair(chain=chain, spot_price=spot_price)
        print("\n" + selected.format_summary() + "\n")
        logger.info("[SUCCESS] ATM Straddle Pair selection verified with live Testnet data.")
        logger.info("=" * 72)
        return 0
    except Exception as e:
        logger.error(f"Selection failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_straddle_selection_cli()))
