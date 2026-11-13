"""
Real-time Market Data Manager and Quote Validation Engine for BTC Options Straddle.

Implements QuoteTick ingestion, rigorous validation (non-zero, uncrossed book, freshness),
straddle snapshot composition, and strict rejection of Last-Traded Price (LTP) as an
ask-side buyback replacement.

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

from nautilus_trader.adapters.bybit.config import (
    BybitDataClientConfig,
    BybitEnvironment,
    BybitProductType,
)
from nautilus_trader.adapters.bybit.factories import (
    BybitLiveDataClientFactory,
    get_cached_bybit_http_client,
    get_cached_bybit_instrument_provider,
)
from nautilus_trader.config import StrategyConfig, TradingNodeConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from config.settings import SecurityError, Settings, get_settings
from exchange.instrument_discovery import BtcOptionsDiscoveryService, OptionContractDetails
from utils.logger import get_logger, setup_logger


@dataclass(frozen=True)
class ValidatedQuote:
    """
    Standardized, validated market quote representing a single instrument's top of book.

    Enforces strict data integrity:
    1. Zero and negative prices are invalid.
    2. Crossed orderbook (bid >= ask) is invalid.
    3. Quotes older than max_quote_age_seconds are stale.
    4. LTP (Last Traded Price) is tracked for reference ONLY and is strictly forbidden
       from substituting ask quotes in buyback valuation.
    """
    instrument_id: str
    bid_price: Optional[float]
    ask_price: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    timestamp_utc: datetime
    ts_event: int  # Nanoseconds from exchange
    ts_init: int   # Nanoseconds when received by Nautilus
    is_valid: bool
    validation_error: Optional[str] = None
    last_traded_price: Optional[float] = None
    ltp_timestamp_utc: Optional[datetime] = None

    @property
    def mid_price(self) -> Optional[float]:
        """Calculate simple midpoint if both bid and ask exist."""
        if self.bid_price is not None and self.ask_price is not None:
            return round((self.bid_price + self.ask_price) / 2.0, 4)
        return None

    @property
    def spread(self) -> Optional[float]:
        """Calculate bid-ask spread."""
        if self.bid_price is not None and self.ask_price is not None:
            return round(self.ask_price - self.bid_price, 4)
        return None

    @property
    def spread_bps(self) -> Optional[float]:
        """Calculate spread in basis points relative to mid."""
        mid = self.mid_price
        spread = self.spread
        if mid and spread and mid > 0:
            return round((spread / mid) * 10_000.0, 2)
        return None

    def age_seconds(self, as_of: Optional[datetime] = None) -> float:
        """Calculate quote age in seconds."""
        now = as_of or datetime.now(timezone.utc)
        return max(0.0, (now - self.timestamp_utc).total_seconds())

    def is_fresh(self, max_age_seconds: float, as_of: Optional[datetime] = None) -> bool:
        """Check if quote is younger than max_age_seconds."""
        return self.age_seconds(as_of=as_of) <= max_age_seconds

    def get_executable_ask(
        self,
        max_age_seconds: float,
        as_of: Optional[datetime] = None,
    ) -> Optional[float]:
        """
        Retrieve actionable ask price for buyback / long entry.

        CRITICAL SAFETY RULE:
        Under no circumstances will last-traded price (LTP) or midpoint be returned
        if the ask quote is missing, zero, crossed, or stale.
        """
        if not self.is_valid:
            return None
        if self.ask_price is None or self.ask_price <= 0.0:
            return None
        if not self.is_fresh(max_age_seconds=max_age_seconds, as_of=as_of):
            return None
        return self.ask_price

    def get_executable_bid(
        self,
        max_age_seconds: float,
        as_of: Optional[datetime] = None,
    ) -> Optional[float]:
        """
        Retrieve actionable bid price for short entry / selling.
        """
        if not self.is_valid:
            return None
        if self.bid_price is None or self.bid_price <= 0.0:
            return None
        if not self.is_fresh(max_age_seconds=max_age_seconds, as_of=as_of):
            return None
        return self.bid_price

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict representation."""
        data = asdict(self)
        data["timestamp_utc"] = self.timestamp_utc.isoformat()
        if self.ltp_timestamp_utc:
            data["ltp_timestamp_utc"] = self.ltp_timestamp_utc.isoformat()
        return data


@dataclass
class StraddleMarketSnapshot:
    """
    Combined real-time market pricing state for the BTC Options Straddle:
    1. Underlying reference price (e.g. BTCUSDT-LINEAR perpetual).
    2. ATM Call option bid & ask.
    3. ATM Put option bid & ask.
    """
    underlying_id: str
    call_id: str
    put_id: str
    as_of_utc: datetime
    max_quote_age_seconds: float
    underlying_quote: Optional[ValidatedQuote] = None
    call_quote: Optional[ValidatedQuote] = None
    put_quote: Optional[ValidatedQuote] = None

    @property
    def underlying_price(self) -> Optional[float]:
        """Underlying mid price or executable price."""
        if self.underlying_quote and self.underlying_quote.is_fresh(self.max_quote_age_seconds):
            return self.underlying_quote.mid_price or self.underlying_quote.bid_price or self.underlying_quote.last_traded_price
        return None

    @property
    def call_bid(self) -> Optional[float]:
        if self.call_quote:
            return self.call_quote.get_executable_bid(self.max_quote_age_seconds, as_of=self.as_of_utc)
        return None

    @property
    def call_ask(self) -> Optional[float]:
        if self.call_quote:
            return self.call_quote.get_executable_ask(self.max_quote_age_seconds, as_of=self.as_of_utc)
        return None

    @property
    def put_bid(self) -> Optional[float]:
        if self.put_quote:
            return self.put_quote.get_executable_bid(self.max_quote_age_seconds, as_of=self.as_of_utc)
        return None

    @property
    def put_ask(self) -> Optional[float]:
        if self.put_quote:
            return self.put_quote.get_executable_ask(self.max_quote_age_seconds, as_of=self.as_of_utc)
        return None

    @property
    def straddle_bid(self) -> Optional[float]:
        """Combined bid price to sell the straddle: call.bid + put.bid."""
        cb = self.call_bid
        pb = self.put_bid
        if cb is not None and pb is not None:
            return round(cb + pb, 4)
        return None

    @property
    def straddle_ask(self) -> Optional[float]:
        """Combined ask price to buy back the straddle: call.ask + put.ask (strictly ask quotes)."""
        ca = self.call_ask
        pa = self.put_ask
        if ca is not None and pa is not None:
            return round(ca + pa, 4)
        return None

    @property
    def straddle_mid(self) -> Optional[float]:
        """Combined straddle midpoint."""
        sb = self.straddle_bid
        sa = self.straddle_ask
        if sb is not None and sa is not None:
            return round((sb + sa) / 2.0, 4)
        return None

    @property
    def is_complete(self) -> bool:
        """True only if all three legs have valid, fresh, actionable quotes."""
        return (
            self.underlying_price is not None
            and self.call_bid is not None
            and self.call_ask is not None
            and self.put_bid is not None
            and self.put_ask is not None
        )

    @property
    def readiness_status(self) -> str:
        """Diagnostic human-readable state string."""
        if self.is_complete:
            return "READY_FOR_EXECUTION"
        reasons = []
        if self.underlying_price is None:
            reasons.append("MISSING_UNDERLYING_PRICE")
        if self.call_bid is None or self.call_ask is None:
            reasons.append("CALL_QUOTE_INCOMPLETE_OR_STALE")
        if self.put_bid is None or self.put_ask is None:
            reasons.append("PUT_QUOTE_INCOMPLETE_OR_STALE")
        return " | ".join(reasons)


class MarketDataManager:
    """
    Manages live QuoteTick and TradeTick ingestion, validation, and real-time state tracking.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        max_quote_age_seconds: Optional[float] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.logger = get_logger("btc_straddle.market_data")
        self.max_quote_age_seconds = (
            max_quote_age_seconds
            if max_quote_age_seconds is not None
            else self.settings.MAX_QUOTE_AGE_SECONDS
        )
        self._quotes: dict[str, ValidatedQuote] = {}
        self._ltp_cache: dict[str, tuple[float, datetime]] = {}

    def validate_and_ingest_quote(self, tick: QuoteTick) -> ValidatedQuote:
        """
        Validate incoming NautilusTrader QuoteTick and update internal state cache.

        Validation Checks:
        1. Parse float bid and ask from Price Cython objects.
        2. Identify missing / zero bids and asks.
        3. Identify negative prices.
        4. Detect crossed orderbook (bid >= ask).
        5. Track exchange latency (ts_init - ts_event).
        """
        inst_id_str = str(tick.instrument_id)

        # Extract values
        bid_val = float(tick.bid_price) if tick.bid_price else 0.0
        ask_val = float(tick.ask_price) if tick.ask_price else 0.0
        bid_sz = float(tick.bid_size) if tick.bid_size else 0.0
        ask_sz = float(tick.ask_size) if tick.ask_size else 0.0

        # Convert nanosecond timestamp to UTC datetime
        ts_event_ns = int(tick.ts_event)
        ts_init_ns = int(tick.ts_init)

        if ts_event_ns > 0:
            ts_utc = datetime.fromtimestamp(ts_event_ns / 1e9, tz=timezone.utc)
        else:
            ts_utc = datetime.now(timezone.utc)

        # Retrieve last known trade price for reference
        ltp, ltp_time = self._ltp_cache.get(inst_id_str, (None, None))

        # Perform rigorous validation
        validation_error: Optional[str] = None
        is_valid = True

        # Check negative prices
        if bid_val < 0.0 or ask_val < 0.0:
            is_valid = False
            validation_error = f"Negative price detected: bid={bid_val}, ask={ask_val}"
            self.logger.error(f"[{inst_id_str}] {validation_error}")

        # Check crossed book
        elif bid_val > 0.0 and ask_val > 0.0 and bid_val >= ask_val:
            is_valid = False
            validation_error = f"Crossed orderbook detected: bid={bid_val} >= ask={ask_val}"
            self.logger.warning(f"[{inst_id_str}] {validation_error}")

        # Treat zero as None (missing quote side)
        actual_bid = bid_val if bid_val > 0.0 else None
        actual_ask = ask_val if ask_val > 0.0 else None

        if actual_bid is None and actual_ask is None:
            is_valid = False
            validation_error = "Both bid and ask are missing or zero."

        validated = ValidatedQuote(
            instrument_id=inst_id_str,
            bid_price=actual_bid,
            ask_price=actual_ask,
            bid_size=bid_sz if actual_bid else None,
            ask_size=ask_sz if actual_ask else None,
            timestamp_utc=ts_utc,
            ts_event=ts_event_ns,
            ts_init=ts_init_ns,
            is_valid=is_valid,
            validation_error=validation_error,
            last_traded_price=ltp,
            ltp_timestamp_utc=ltp_time,
        )

        self._quotes[inst_id_str] = validated

        if is_valid:
            self.logger.debug(
                f"[{inst_id_str}] ValidatedQuote: bid={validated.bid_price} | ask={validated.ask_price} | spread={validated.spread}"
            )

        return validated

    def ingest_trade(self, trade: TradeTick) -> None:
        """
        Record underlying or option trade tick for LTP reference.
        NOTE: LTP is strictly separated from top-of-book ask quotes.
        """
        inst_id_str = str(trade.instrument_id)
        price = float(trade.price)
        ts_ns = int(trade.ts_event)
        ts_utc = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc) if ts_ns > 0 else datetime.now(timezone.utc)

        self._ltp_cache[inst_id_str] = (price, ts_utc)

        # Update cached quote LTP if quote exists
        if inst_id_str in self._quotes:
            existing = self._quotes[inst_id_str]
            self._quotes[inst_id_str] = ValidatedQuote(
                instrument_id=existing.instrument_id,
                bid_price=existing.bid_price,
                ask_price=existing.ask_price,
                bid_size=existing.bid_size,
                ask_size=existing.ask_size,
                timestamp_utc=existing.timestamp_utc,
                ts_event=existing.ts_event,
                ts_init=existing.ts_init,
                is_valid=existing.is_valid,
                validation_error=existing.validation_error,
                last_traded_price=price,
                ltp_timestamp_utc=ts_utc,
            )

    def get_quote(self, instrument_id: str) -> Optional[ValidatedQuote]:
        """Retrieve latest validated quote for an instrument."""
        return self._quotes.get(instrument_id)

    def get_straddle_snapshot(
        self,
        underlying_id: str,
        call_id: str,
        put_id: str,
        as_of_utc: Optional[datetime] = None,
    ) -> StraddleMarketSnapshot:
        """
        Generate instantaneous StraddleMarketSnapshot consolidating the 3 required legs.
        """
        now = as_of_utc or datetime.now(timezone.utc)
        return StraddleMarketSnapshot(
            underlying_id=underlying_id,
            call_id=call_id,
            put_id=put_id,
            as_of_utc=now,
            max_quote_age_seconds=self.max_quote_age_seconds,
            underlying_quote=self._quotes.get(underlying_id),
            call_quote=self._quotes.get(call_id),
            put_quote=self._quotes.get(put_id),
        )


class MarketDataDiagnosticStrategy(Strategy):
    """
    NautilusTrader diagnostic strategy to inspect and verify real-time market data
    subscriptions for BTC underlying, Call, and Put options on Bybit Testnet.

    Zero trading orders are placed by this strategy.
    """

    def __init__(
        self,
        config: StrategyConfig,
        underlying_id: InstrumentId,
        call_id: InstrumentId,
        put_id: InstrumentId,
        market_data_manager: MarketDataManager,
    ) -> None:
        super().__init__(config=config)
        self.underlying_id = underlying_id
        self.call_id = call_id
        self.put_id = put_id
        self.mgr = market_data_manager
        self.tick_count = 0
        self.ticks_by_instrument: dict[str, int] = {
            str(underlying_id): 0,
            str(call_id): 0,
            str(put_id): 0,
        }

    def on_start(self) -> None:
        """Subscribe to quote and trade ticks for all 3 required instruments."""
        self._log.info("MarketDataDiagnosticStrategy starting subscriptions...")

        # 1. Subscribe to underlying reference
        self._log.info(f"Subscribing to Underlying: {self.underlying_id}")
        self.subscribe_quote_ticks(self.underlying_id)
        self.subscribe_trade_ticks(self.underlying_id)

        # 2. Subscribe to Call option
        self._log.info(f"Subscribing to Call Option: {self.call_id}")
        self.subscribe_quote_ticks(self.call_id)

        # 3. Subscribe to Put option
        self._log.info(f"Subscribing to Put Option: {self.put_id}")
        self.subscribe_quote_ticks(self.put_id)

        self._log.info("All market-data subscriptions registered.")

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Handle incoming top-of-book quote tick."""
        self.tick_count += 1
        inst_str = str(tick.instrument_id)
        self.ticks_by_instrument[inst_str] = self.ticks_by_instrument.get(inst_str, 0) + 1

        # Ingest and validate quote
        validated = self.mgr.validate_and_ingest_quote(tick)

        # Log with distinguished prefixes
        if inst_str == str(self.underlying_id):
            tag = "[REF-UNDERLYING]"
        elif inst_str == str(self.call_id):
            tag = "[CALL-OPTION]"
        elif inst_str == str(self.put_id):
            tag = "[PUT-OPTION]"
        else:
            tag = "[OTHER]"

        bid_repr = f"${validated.bid_price:,.2f}" if validated.bid_price else "NO_BID"
        ask_repr = f"${validated.ask_price:,.2f}" if validated.ask_price else "NO_ASK"
        spread_repr = f"spread=${validated.spread:,.2f}" if validated.spread else "no-spread"

        self._log.info(
            f"{tag} {inst_str} | BID: {bid_repr} | ASK: {ask_repr} | {spread_repr} | age={validated.age_seconds():.2f}s"
        )

        # Evaluate straddle snapshot
        snapshot = self.mgr.get_straddle_snapshot(
            underlying_id=str(self.underlying_id),
            call_id=str(self.call_id),
            put_id=str(self.put_id),
        )

        if snapshot.is_complete:
            self._log.info(
                f"[STRADDLE-SNAPSHOT-COMPLETE] Underlying: ${snapshot.underlying_price:,.2f} | "
                f"Straddle BID: ${snapshot.straddle_bid:,.2f} | "
                f"Straddle ASK: ${snapshot.straddle_ask:,.2f} | "
                f"Mid: ${snapshot.straddle_mid:,.2f}"
            )
        else:
            self._log.debug(f"[STRADDLE-STATUS] {snapshot.readiness_status}")

    def on_trade_tick(self, trade: TradeTick) -> None:
        """Handle incoming trade tick."""
        self.mgr.ingest_trade(trade)
        self._log.debug(
            f"[TRADE] {trade.instrument_id} @ ${float(trade.price):,.2f} (sz: {float(trade.size)})"
        )

    def on_stop(self) -> None:
        """Log diagnostic wrap-up summary."""
        self._log.info("MarketDataDiagnosticStrategy stopping...")
        self._log.info(f"Total QuoteTicks received: {self.tick_count}")
        for inst_str, count in self.ticks_by_instrument.items():
            self._log.info(f"  {inst_str}: {count} ticks")


async def run_market_data_diagnostic(
    duration_seconds: float = 15.0,
    underlying_symbol: str = "BTCUSDT-LINEAR.BYBIT",
) -> int:
    """
    Run the market-data subscription diagnostic on Bybit Testnet.

    1. Discovers active BTC options and selects the nearest ATM straddle pair.
    2. Builds TradingNode with public WebSocket feeds for Option & Linear.
    3. Runs diagnostic strategy for duration_seconds.
    4. Gracefully disconnects with zero orders submitted.
    """
    logger = setup_logger("btc_straddle.market_data")
    logger.info("=" * 72)
    logger.info("BYBIT TESTNET - REAL-TIME MARKET DATA DIAGNOSTIC (PART 6)")
    logger.info("=" * 72)

    settings = get_settings()
    discovery = BtcOptionsDiscoveryService(settings=settings)

    # 1. Load active options chain
    chain = await discovery.discover_btc_options(active_only=True)
    if not chain.expirations:
        logger.error("No active options found to subscribe to.")
        return 1

    # 2. Select first unexpired expiration date and its nearest ATM strike
    target_exp = chain.expirations[0]
    strikes = chain.strikes_by_expiry.get(target_exp, [])
    if not strikes:
        logger.error(f"No strikes found for expiration {target_exp}.")
        return 1

    # Pick middle strike as ATM candidate
    mid_strike = strikes[len(strikes) // 2]
    call_leg, put_leg = chain.get_straddle_pair(target_exp, mid_strike)

    if not call_leg or not put_leg:
        # Fallback to first available pair
        for k in strikes:
            c, p = chain.get_straddle_pair(target_exp, k)
            if c and p:
                call_leg, put_leg = c, p
                mid_strike = k
                break

    if not call_leg or not put_leg:
        logger.error("Could not find a matched Call/Put straddle pair.")
        return 1

    logger.info(f"Selected Target Expiry: {target_exp.strftime('%Y-%m-%d %H:%M UTC')}")
    logger.info(f"Selected Strike Price : ${mid_strike:,.0f}")
    logger.info(f"Call Instrument ID   : {call_leg.instrument_id}")
    logger.info(f"Put Instrument ID    : {put_leg.instrument_id}")

    # 3. Load underlying Linear perpetual
    client = get_cached_bybit_http_client(environment=BybitEnvironment.TESTNET)
    lin_provider = get_cached_bybit_instrument_provider(client, (BybitProductType.LINEAR,))
    await lin_provider.load_all_async(filters={"base_coin": "BTC"})
    underlying_inst = next(
        (i for i in lin_provider.list_all() if str(i.id).startswith("BTCUSDT-LINEAR")),
        None,
    )

    if not underlying_inst:
        logger.error(f"Could not load underlying perpetual instrument {underlying_symbol}")
        return 1

    logger.info(f"Underlying Instrument : {underlying_inst.id}")

    # 4. Build TradingNode
    data_cfg = BybitDataClientConfig(
        environment=BybitEnvironment.TESTNET,
        product_types=(BybitProductType.OPTION, BybitProductType.LINEAR),
    )
    node_cfg = TradingNodeConfig(
        trader_id="MARKET-DATA-DIAG-NODE",
        data_clients={"BYBIT": data_cfg},
        exec_clients={},
        timeout_connection=10.0,
    )
    node = TradingNode(config=node_cfg)
    node.add_data_client_factory("BYBIT", BybitLiveDataClientFactory)
    node.build()

    # 5. Populate Node Cache with Instruments
    node.cache.add_instrument(underlying_inst)
    if call_leg.nautilus_instrument:
        node.cache.add_instrument(call_leg.nautilus_instrument)
    if put_leg.nautilus_instrument:
        node.cache.add_instrument(put_leg.nautilus_instrument)

    # 6. Initialize MarketDataManager and Mount Strategy
    mgr = MarketDataManager(settings=settings)
    strat = MarketDataDiagnosticStrategy(
        config=StrategyConfig(strategy_id="MKT-DATA-DIAG-01"),
        underlying_id=underlying_inst.id,
        call_id=InstrumentId.from_str(call_leg.instrument_id),
        put_id=InstrumentId.from_str(put_leg.instrument_id),
        market_data_manager=mgr,
    )
    node.trader.add_strategy(strat)

    # 7. Execute Streaming Window
    logger.info(f"Starting real-time quote streaming diagnostic ({duration_seconds}s window)...")
    run_task = asyncio.create_task(node.run_async())

    try:
        await asyncio.sleep(duration_seconds)
    finally:
        logger.info("Initiating graceful diagnostic shutdown...")
        await node.stop_async()
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):
            pass

    logger.info("-" * 72)
    logger.info(f"DIAGNOSTIC COMPLETE: Received {strat.tick_count} quote ticks.")
    logger.info("System disconnected cleanly with zero orders submitted.")
    logger.info("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run_market_data_diagnostic(duration_seconds=12.0)))
