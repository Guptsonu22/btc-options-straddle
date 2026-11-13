"""
Unit tests for Market Data Management, Quote Validation, and Straddle Snapshot Engine.

Verifies:
1. Parsing and validation of QuoteTick top-of-book events.
2. Non-zero, non-negative price enforcement.
3. Crossed orderbook detection (bid >= ask).
4. Timestamp freshness and quote staleness detection.
5. Strict rejection of Last-Traded Price (LTP) as an ask-side buyback replacement.
6. Combined straddle bid, ask, and readiness status calculations.
"""

from datetime import datetime, timedelta, timezone
import pytest

from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.objects import Price, Quantity

from config.settings import Settings
from exchange.market_data import (
    MarketDataManager,
    StraddleMarketSnapshot,
    ValidatedQuote,
)


def make_test_quote_tick(
    symbol_str: str,
    bid_val: float,
    ask_val: float,
    bid_size_val: float = 1.0,
    ask_size_val: float = 1.0,
    age_seconds: float = 0.5,
    precision: int = 2,
) -> QuoteTick:
    """Helper to construct authentic NautilusTrader QuoteTick instances."""
    sym = Symbol(symbol_str)
    inst_id = InstrumentId(symbol=sym, venue=Venue("BYBIT"))
    now = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ts_ns = int(now.timestamp() * 1e9)

    bid_p = Price.from_str(f"{bid_val:.{precision}f}")
    ask_p = Price.from_str(f"{ask_val:.{precision}f}")
    bid_s = Quantity.from_str(f"{bid_size_val:.2f}")
    ask_s = Quantity.from_str(f"{ask_size_val:.2f}")

    return QuoteTick(
        instrument_id=inst_id,
        bid_price=bid_p,
        ask_price=ask_p,
        bid_size=bid_s,
        ask_size=ask_s,
        ts_event=ts_ns,
        ts_init=ts_ns + 5_000_000,  # +5ms arrival
    )


@pytest.fixture
def market_data_manager() -> MarketDataManager:
    settings = Settings(
        BYBIT_TESTNET=True,
        MAX_QUOTE_AGE_SECONDS=5.0,
        DRY_RUN=True,
    )
    return MarketDataManager(settings=settings, max_quote_age_seconds=5.0)


def test_valid_quote_ingestion(market_data_manager: MarketDataManager):
    """Verify normal two-sided quote calculation of mid, spread, and spread_bps."""
    tick = make_test_quote_tick(
        symbol_str="BTCUSDT-LINEAR",
        bid_val=85000.0,
        ask_val=85005.0,
        age_seconds=0.1,
    )

    quote = market_data_manager.validate_and_ingest_quote(tick)

    assert quote.is_valid is True
    assert quote.bid_price == 85000.0
    assert quote.ask_price == 85005.0
    assert quote.mid_price == 85002.5
    assert quote.spread == 5.0
    assert quote.spread_bps == pytest.approx(0.588, rel=1e-2)
    assert quote.is_fresh(max_age_seconds=5.0) is True


def test_crossed_orderbook_detection(market_data_manager: MarketDataManager):
    """Verify that inverted/crossed orderbook (bid >= ask) is flagged invalid."""
    crossed_tick = make_test_quote_tick(
        symbol_str="BTC-28MAR25-85000-C-USDT",
        bid_val=3500.0,
        ask_val=3450.0,  # bid > ask!
        age_seconds=0.1,
    )

    quote = market_data_manager.validate_and_ingest_quote(crossed_tick)

    assert quote.is_valid is False
    assert "Crossed orderbook detected" in (quote.validation_error or "")
    assert quote.get_executable_ask(max_age_seconds=5.0) is None
    assert quote.get_executable_bid(max_age_seconds=5.0) is None


def test_stale_quote_rejection(market_data_manager: MarketDataManager):
    """Verify that quotes older than max_quote_age_seconds are rejected for execution."""
    stale_tick = make_test_quote_tick(
        symbol_str="BTC-28MAR25-85000-P-USDT",
        bid_val=2200.0,
        ask_val=2250.0,
        age_seconds=10.0,  # 10s old, limit is 5s
    )

    quote = market_data_manager.validate_and_ingest_quote(stale_tick)

    assert quote.is_valid is True  # Mathematically valid, but stale
    assert quote.is_fresh(max_age_seconds=5.0) is False
    assert quote.get_executable_ask(max_age_seconds=5.0) is None
    assert quote.get_executable_bid(max_age_seconds=5.0) is None


def test_missing_quote_side_handling(market_data_manager: MarketDataManager):
    """Verify one-sided quotes (missing bid or ask) are handled safely without crashing."""
    # Ask only quote (e.g. deep OTM put with no buyers)
    ask_only_tick = make_test_quote_tick(
        symbol_str="BTC-28MAR25-30000-P-USDT",
        bid_val=0.0,  # Zero bid
        ask_val=50.0,
        age_seconds=0.2,
    )

    quote = market_data_manager.validate_and_ingest_quote(ask_only_tick)

    assert quote.is_valid is True
    assert quote.bid_price is None
    assert quote.ask_price == 50.0
    assert quote.mid_price is None  # Cannot compute midpoint with 1 side
    assert quote.get_executable_bid(max_age_seconds=5.0) is None
    assert quote.get_executable_ask(max_age_seconds=5.0) == 50.0


def test_strict_rejection_of_ltp_for_ask_side_buyback(market_data_manager: MarketDataManager):
    """
    CRITICAL SAFETY REQUIREMENT:
    Verify that Last-Traded Price (LTP) is NEVER used as a substitute for ask-side buyback quotes.
    """
    inst_str = "BTC-28MAR25-85000-C-USDT.BYBIT"
    inst_id = InstrumentId.from_str(inst_str)

    # 1. Ingest a trade tick (LTP = $3,000.00)
    ts_now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    trade = TradeTick(
        inst_id,
        Price.from_str("3000.00"),
        Quantity.from_str("0.50"),
        AggressorSide.BUYER,
        TradeId("T-987654321"),
        ts_now_ns,
        ts_now_ns,
    )
    market_data_manager.ingest_trade(trade)

    # 2. Case A: No quote exists yet -> get_executable_ask must be None
    q0 = market_data_manager.get_quote(inst_str)
    assert q0 is None

    # 3. Case B: Quote has bid ($2,900) but MISSING ASK (ask = 0.0)
    bid_only_tick = make_test_quote_tick(
        symbol_str="BTC-28MAR25-85000-C-USDT",
        bid_val=2900.0,
        ask_val=0.0,
        age_seconds=0.1,
    )
    q1 = market_data_manager.validate_and_ingest_quote(bid_only_tick)

    assert q1.last_traded_price == 3000.0
    assert q1.ask_price is None
    # MUST return None, NOT 3000.0!
    assert q1.get_executable_ask(max_age_seconds=5.0) is None

    # 4. Case C: Quote ask is STALE (15 seconds old)
    stale_tick = make_test_quote_tick(
        symbol_str="BTC-28MAR25-85000-C-USDT",
        bid_val=2900.0,
        ask_val=3100.0,
        age_seconds=15.0,
    )
    q2 = market_data_manager.validate_and_ingest_quote(stale_tick)
    # Stale ask must NOT fall back to fresh LTP!
    assert q2.get_executable_ask(max_age_seconds=5.0) is None


def test_straddle_market_snapshot_composition(market_data_manager: MarketDataManager):
    """Verify StraddleMarketSnapshot correctly sums legs and monitors completeness."""
    now = datetime.now(timezone.utc)

    underlying_id = "BTCUSDT-LINEAR.BYBIT"
    call_id = "BTC-28MAR25-85000-C-USDT.BYBIT"
    put_id = "BTC-28MAR25-85000-P-USDT.BYBIT"

    # Ingest underlying quote
    market_data_manager.validate_and_ingest_quote(
        make_test_quote_tick("BTCUSDT-LINEAR", 85000.0, 85002.0, age_seconds=0.1)
    )

    # Ingest call quote
    market_data_manager.validate_and_ingest_quote(
        make_test_quote_tick("BTC-28MAR25-85000-C-USDT", 2500.0, 2550.0, age_seconds=0.1)
    )

    # Snapshot with missing put -> incomplete
    snap1 = market_data_manager.get_straddle_snapshot(underlying_id, call_id, put_id, as_of_utc=now)
    assert snap1.is_complete is False
    assert "PUT_QUOTE_INCOMPLETE" in snap1.readiness_status
    assert snap1.straddle_bid is None
    assert snap1.straddle_ask is None

    # Ingest put quote
    market_data_manager.validate_and_ingest_quote(
        make_test_quote_tick("BTC-28MAR25-85000-P-USDT", 2100.0, 2150.0, age_seconds=0.1)
    )

    # Complete snapshot
    snap2 = market_data_manager.get_straddle_snapshot(underlying_id, call_id, put_id, as_of_utc=now)
    assert snap2.is_complete is True
    assert snap2.readiness_status == "READY_FOR_EXECUTION"
    assert snap2.call_bid == 2500.0
    assert snap2.call_ask == 2550.0
    assert snap2.put_bid == 2100.0
    assert snap2.put_ask == 2150.0
    # Combined straddle bid: 2500 + 2100 = 4600.0
    assert snap2.straddle_bid == 4600.0
    # Combined straddle ask: 2550 + 2150 = 4700.0
    assert snap2.straddle_ask == 4700.0
    assert snap2.straddle_mid == 4650.0
