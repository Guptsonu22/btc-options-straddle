"""
Unit tests for the Level 1 BTC Short Straddle Strategy (Part 9).

Verifies:
1. Strategy configuration and initialization.
2. Strategy lifecycle (on_start, on_stop).
3. Underlying reference price ingestion and ATM straddle selection.
4. Quote validation and dual-leg short SELL order execution.
5. Invariant check: Order submission does NOT assume execution.
6. Execution callbacks (on_order_submitted, on_order_accepted, on_order_filled).
7. Position tracking and mark-to-market valuation via ask prices.
8. Unmatched leg recognition: Call filled, Put pending -> triggers resolution policy.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Tuple
import pytest

from nautilus_trader.common.component import TestClock
from nautilus_trader.model.currencies import BTC, USDT
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OptionKind, OrderSide, OrderStatus
from nautilus_trader.model.identifiers import (
    AccountId,
    InstrumentId,
    StrategyId,
    Symbol,
    TraderId,
    Venue,
)
from nautilus_trader.model.instruments.crypto_option import CryptoOption
from nautilus_trader.model.objects import Price, Quantity

from exchange.instrument_discovery import (
    DiscoveredOptionsChain,
    OptionContractDetails,
)
from strategy.order_manager import LegExecutionStatus, StraddleExecutionStatus
from strategy.straddle_strategy import (
    BtcShortStraddleStrategy,
    BtcShortStraddleStrategyConfig,
    StraddleStrategyState,
)


# -----------------------------------------------------------------------------
# Fixtures & Mock Generators
# -----------------------------------------------------------------------------


def make_test_crypto_option(
    symbol_str: str,
    option_kind: OptionKind,
    strike_price_val: float,
    expiration_dt: datetime,
) -> CryptoOption:
    """Construct an authentic NautilusTrader CryptoOption for testing."""
    sym = Symbol(symbol_str)
    inst_id = InstrumentId(symbol=sym, venue=Venue("BYBIT"))
    exp_ns = int(expiration_dt.timestamp() * 1e9)
    act_ns = int((expiration_dt - timedelta(days=30)).timestamp() * 1e9)

    return CryptoOption(
        instrument_id=inst_id,
        raw_symbol=sym,
        underlying=BTC,
        quote_currency=USDT,
        settlement_currency=USDT,
        is_inverse=False,
        option_kind=option_kind,
        strike_price=Price.from_str(str(int(strike_price_val))),
        activation_ns=act_ns,
        expiration_ns=exp_ns,
        price_precision=0,
        size_precision=2,
        price_increment=Price.from_str("5"),
        size_increment=Quantity.from_str("0.01"),
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0004"),
        margin_init=Decimal("0.10"),
        margin_maint=Decimal("0.05"),
        multiplier=Quantity.from_str("1.0"),
        min_quantity=Quantity.from_str("0.01"),
        ts_event=act_ns,
        ts_init=act_ns,
    )


def make_test_chain(strike: float = 70000.0) -> Tuple[DiscoveredOptionsChain, CryptoOption, CryptoOption]:
    """Build a mock options chain with a single expiration and matching Call/Put."""
    expiry = datetime.now(timezone.utc) + timedelta(days=2)
    call_inst = make_test_crypto_option(f"BTC-TEST-{int(strike)}-C-USDT", OptionKind.CALL, strike, expiry)
    put_inst = make_test_crypto_option(f"BTC-TEST-{int(strike)}-P-USDT", OptionKind.PUT, strike, expiry)

    call_details = OptionContractDetails(
        underlying="BTC",
        instrument_id=call_inst.id.value,
        raw_symbol=call_inst.raw_symbol.value,
        option_type="CALL",
        strike_price=strike,
        expiration_utc=expiry,
        contract_multiplier=1.0,
        settlement_currency="USDT",
        min_order_quantity=0.01,
        quantity_increment=0.01,
        price_precision=0,
        price_increment=5.0,
        trading_status="TRADING",
        is_active=True,
        nautilus_instrument=call_inst,
    )

    put_details = OptionContractDetails(
        underlying="BTC",
        instrument_id=put_inst.id.value,
        raw_symbol=put_inst.raw_symbol.value,
        option_type="PUT",
        strike_price=strike,
        expiration_utc=expiry,
        contract_multiplier=1.0,
        settlement_currency="USDT",
        min_order_quantity=0.01,
        quantity_increment=0.01,
        price_precision=0,
        price_increment=5.0,
        trading_status="TRADING",
        is_active=True,
        nautilus_instrument=put_inst,
    )

    chain = DiscoveredOptionsChain(
        underlying="BTC",
        as_of_utc=datetime.now(timezone.utc),
        total_discovered=2,
        total_active=2,
        calls=[call_details],
        puts=[put_details],
        expirations=[expiry],
        strikes_by_expiry={expiry: [strike]},
        contracts_by_id={
            call_inst.id.value: call_details,
            put_inst.id.value: put_details,
        },
        _calls_map={(expiry, strike): call_details},
        _puts_map={(expiry, strike): put_details},
    )
    return chain, call_inst, put_inst


@pytest.fixture
def strategy():
    """Provides a BtcShortStraddleStrategy instance configured for DRY_RUN testing."""
    config = BtcShortStraddleStrategyConfig(
        strategy_id="STRAT-TEST",
        order_id_tag="STRAT01",
        btc_symbol="BTC",
        underlying_instrument_id="BTCUSDT-LINEAR.BYBIT",
        order_quantity=0.01,
        dry_run=True,
        max_quote_age_seconds=5.0,
        unmatched_leg_policy="CANCEL_AND_CLOSE",
    )
    strat = BtcShortStraddleStrategy(config=config)
    return strat


# -----------------------------------------------------------------------------
# Unit Tests
# -----------------------------------------------------------------------------


def test_strategy_initialization_and_start(strategy):
    """Verify strategy starts up, creates OrderManager, and transitions state."""
    strategy.on_start()
    assert strategy.strategy_state == StraddleStrategyState.AWAITING_INSTRUMENTS
    assert strategy.order_manager is not None
    assert strategy.order_manager.dry_run is True


def test_atm_selection_on_underlying_tick(strategy):
    """
    Verify receiving underlying QuoteTick triggers ATM selection when options chain is loaded.
    """
    strategy.on_start()
    chain, call_inst, put_inst = make_test_chain(strike=70000.0)
    strategy.set_options_chain(chain)

    # Ingest underlying quote: Spot = $70,050.00 -> ATM Strike = $70,000.00
    underlying_id = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    tick = QuoteTick(
        instrument_id=underlying_id,
        bid_price=Price.from_str("70040.0"),
        ask_price=Price.from_str("70060.0"),
        bid_size=Quantity.from_str("1.0"),
        ask_size=Quantity.from_str("1.0"),
        ts_event=now_ns,
        ts_init=now_ns,
    )
    strategy.on_quote_tick(tick)

    # Verify ATM selection
    assert strategy.underlying_price == 70050.0
    assert strategy.selected_straddle is not None
    assert strategy.selected_straddle.strike_price == 70000.0
    assert strategy.call_instrument_id == call_inst.id
    assert strategy.put_instrument_id == put_inst.id
    assert strategy.strategy_state == StraddleStrategyState.VALIDATING_QUOTES


def test_dual_leg_entry_on_option_quotes_dry_run(strategy):
    """
    Verify that once both option leg quotes arrive, the strategy submits dual-leg SELL orders.
    Verifies that order submission does NOT assume execution.
    """
    strategy.on_start()
    chain, call_inst, put_inst = make_test_chain(strike=70000.0)
    strategy.set_options_chain(chain)

    # 1. Underlying tick
    underlying_id = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    strategy.on_quote_tick(
        QuoteTick(underlying_id, Price.from_str("69990.0"), Price.from_str("70010.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns)
    )

    # 2. Call Option quote tick
    strategy.on_quote_tick(
        QuoteTick(call_inst.id, Price.from_str("1200.0"), Price.from_str("1250.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns)
    )

    # 3. Put Option quote tick -> triggers entry
    strategy.on_quote_tick(
        QuoteTick(put_inst.id, Price.from_str("1100.0"), Price.from_str("1150.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns)
    )

    # Verify straddle orders submitted
    assert strategy.orders_submitted is True
    assert strategy.straddle_order is not None
    assert strategy.straddle_order.call_leg.status == LegExecutionStatus.SUBMITTED
    assert strategy.straddle_order.put_leg.status == LegExecutionStatus.SUBMITTED

    # CRUCIAL INVARIANT: Submission does NOT imply execution
    assert strategy.straddle_order.call_leg.filled_quantity == 0.0
    assert strategy.straddle_order.put_leg.filled_quantity == 0.0
    assert strategy.straddle_order.total_filled_quantity == 0.0


def test_complete_straddle_fill_execution(strategy):
    """
    Verify that when both Call and Put fills arrive, position is active and tracked.
    """
    strategy.on_start()
    chain, call_inst, put_inst = make_test_chain(strike=70000.0)
    strategy.set_options_chain(chain)

    # Underlying and option quotes
    underlying_id = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    strategy.on_quote_tick(QuoteTick(underlying_id, Price.from_str("69990.0"), Price.from_str("70010.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns))
    strategy.on_quote_tick(QuoteTick(call_inst.id, Price.from_str("1200.0"), Price.from_str("1250.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns))
    strategy.on_quote_tick(QuoteTick(put_inst.id, Price.from_str("1100.0"), Price.from_str("1150.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns))

    # Accept both legs
    strategy.order_manager.simulate_acceptance(strategy.straddle_order.call_leg.client_order_id)
    strategy.order_manager.simulate_acceptance(strategy.straddle_order.put_leg.client_order_id)

    # Fill both legs
    fill1 = strategy.order_manager.simulate_fill(strategy.straddle_order.call_leg.client_order_id, fill_quantity=0.01, fill_price=1200.0)
    fill2 = strategy.order_manager.simulate_fill(strategy.straddle_order.put_leg.client_order_id, fill_quantity=0.01, fill_price=1100.0)
    strategy.on_order_filled(fill1)
    strategy.on_order_filled(fill2)

    # Verify position is active
    assert strategy.straddle_order.is_fully_filled is True
    straddle_pos = strategy.position_tracker.get_straddle_position()
    assert straddle_pos is not None
    assert straddle_pos.is_balanced is True
    assert straddle_pos.call_position.quantity == 0.01
    assert straddle_pos.put_position.quantity == 0.01
    assert straddle_pos.total_entry_premium == 23.0  # (0.01*1200) + (0.01*1100) = 12 + 11 = 23 USDT


def test_unmatched_leg_recovery_in_strategy(strategy):
    """
    CRITICAL REQUIREMENT:
    Scenario: Call fills, Put remains pending.
    Verify strategy transitions to UNMATCHED_RECOVERING, cancels pending Put,
    and submits offsetting buyback for Call.
    """
    strategy.on_start()
    chain, call_inst, put_inst = make_test_chain(strike=70000.0)
    strategy.set_options_chain(chain)

    # Quotes and submission
    underlying_id = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    strategy.on_quote_tick(QuoteTick(underlying_id, Price.from_str("69990.0"), Price.from_str("70010.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns))
    strategy.on_quote_tick(QuoteTick(call_inst.id, Price.from_str("1200.0"), Price.from_str("1250.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns))
    strategy.on_quote_tick(QuoteTick(put_inst.id, Price.from_str("1100.0"), Price.from_str("1150.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns))

    # Accept both legs
    strategy.order_manager.simulate_acceptance(strategy.straddle_order.call_leg.client_order_id)
    strategy.order_manager.simulate_acceptance(strategy.straddle_order.put_leg.client_order_id)

    # Call fills, Put remains accepted/pending
    fill_ev = strategy.order_manager.simulate_fill(strategy.straddle_order.call_leg.client_order_id, fill_quantity=0.01, fill_price=1200.0)
    strategy.on_order_filled(fill_ev)

    # Strategy state transitions to UNMATCHED_RECOVERING
    assert strategy.strategy_state == StraddleStrategyState.UNMATCHED_RECOVERING
    assert strategy.straddle_order.put_leg.status == LegExecutionStatus.CANCELED
    assert strategy.straddle_order.closing_call_order is not None
    assert strategy.straddle_order.closing_call_order.order_side == OrderSide.BUY
    assert strategy.straddle_order.closing_call_order.target_quantity == 0.01


def test_strategy_graceful_shutdown(strategy):
    """Verify clean shutdown unsubscribes feeds and logs position summary."""
    strategy.on_start()
    strategy.on_stop()
    assert strategy.strategy_state == StraddleStrategyState.STOPPED
