"""
Unit tests for the Order Management Engine (Part 8).

Verifies:
1. Unique client order ID generation (uniqueness, monotonicity, Bybit length limits).
2. Short straddle SELL order creation with NautilusTrader LimitOrder and MarketOrder.
3. Strict quantity validation (positive, min lot size, lot size step increments).
4. Order submission in DRY_RUN mode vs execution client submission.
5. State machine transitions:
   NOT_SUBMITTED -> SUBMITTED -> ACCEPTED -> PARTIALLY_FILLED -> FILLED / CANCELED / REJECTED.
6. Accurate cumulative fill quantity and volume-weighted average price (VWAP) calculation.
7. Duplicate order and duplicate client order ID prevention.
8. Independent tracking of Call and Put option legs.
9. CRITICAL SCENARIO: Call fully filled, Put pending -> system recognizes ONE_LEG_FILLED.
10. Configurable unmatched-leg policies:
    - WAIT: continues awaiting second leg.
    - CANCEL_REMAINING: cancels the pending leg.
    - CANCEL_AND_CLOSE: cancels pending leg and submits offsetting BUY order for filled leg.
11. Non-assumption of execution upon submission (submission does not increment fill quantity).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from nautilus_trader.common.component import TestClock
from nautilus_trader.model.currencies import BTC, USDT
from nautilus_trader.model.enums import (
    LiquiditySide,
    OptionKind,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    StrategyId,
    Symbol,
    TradeId,
    TraderId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.instruments.crypto_option import CryptoOption
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.core.uuid import UUID4

from strategy.order_manager import (
    DuplicateOrderError,
    InvalidQuantityError,
    LegExecutionStatus,
    LegOrderTracker,
    OptionLegType,
    OrderManagementError,
    OrderManager,
    OrderSubmissionError,
    StraddleExecutionStatus,
    StraddleOrder,
    UnmatchedLegPolicy,
)


# -----------------------------------------------------------------------------
# Fixtures & Helpers
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


@pytest.fixture
def test_instruments():
    """Provides a paired Call and Put CryptoOption with identical strike and expiry."""
    expiry = datetime.now(timezone.utc) + timedelta(days=2)
    call_inst = make_test_crypto_option("BTC-TEST-70000-C-USDT", OptionKind.CALL, 70000.0, expiry)
    put_inst = make_test_crypto_option("BTC-TEST-70000-P-USDT", OptionKind.PUT, 70000.0, expiry)
    return call_inst, put_inst


@pytest.fixture
def order_manager():
    """Provides a fresh OrderManager instance running in DRY_RUN mode with TestClock."""
    clock = TestClock()
    return OrderManager(
        trader_id=TraderId("TRADER-TEST"),
        strategy_id=StrategyId("STRAT-TEST"),
        account_id=AccountId("BYBIT-TESTNET-001"),
        unmatched_policy=UnmatchedLegPolicy.CANCEL_AND_CLOSE,
        dry_run=True,
        clock=clock,
    )


# -----------------------------------------------------------------------------
# Unit Tests
# -----------------------------------------------------------------------------


def test_unique_client_order_id_generation(order_manager):
    """
    Test 1: Verify unique client order ID generation.
    - Monotonic, collision-free, <= 36 characters (Bybit V5 requirement).
    - Prefixed with 'S-C-' for Call, 'S-P-' for Put.
    """
    call_ids = [order_manager.generate_client_order_id(OptionLegType.CALL) for _ in range(50)]
    put_ids = [order_manager.generate_client_order_id(OptionLegType.PUT) for _ in range(50)]

    all_ids = [cid.value for cid in call_ids + put_ids]
    # Check uniqueness
    assert len(all_ids) == len(set(all_ids)), "Client order IDs must be strictly unique"

    # Check Bybit constraints and prefixes
    for cid in call_ids:
        assert cid.value.startswith("S-C-")
        assert len(cid.value) <= 36, f"Client order ID '{cid.value}' exceeds 36 chars"

    for cid in put_ids:
        assert cid.value.startswith("S-P-")
        assert len(cid.value) <= 36, f"Client order ID '{cid.value}' exceeds 36 chars"


def test_quantity_validation(order_manager):
    """
    Test 2: Verify quantity validation against lot size, step, and positive constraints.
    """
    # Valid quantities
    assert order_manager.validate_order_quantity(0.01, min_lot_size=0.01, lot_size_step=0.01) == 0.01
    assert order_manager.validate_order_quantity(0.05, min_lot_size=0.01, lot_size_step=0.01) == 0.05
    assert order_manager.validate_order_quantity(1.50, min_lot_size=0.01, lot_size_step=0.01) == 1.50

    # Non-positive quantity
    with pytest.raises(InvalidQuantityError, match="positive"):
        order_manager.validate_order_quantity(0.0)

    with pytest.raises(InvalidQuantityError, match="positive"):
        order_manager.validate_order_quantity(-0.05)

    # Below minimum lot size
    with pytest.raises(InvalidQuantityError, match="less than minimum lot size"):
        order_manager.validate_order_quantity(0.005, min_lot_size=0.01)

    # Invalid step increment (e.g. 0.015 when step is 0.01)
    with pytest.raises(InvalidQuantityError, match="does not align with lot step"):
        order_manager.validate_order_quantity(0.015, min_lot_size=0.01, lot_size_step=0.01)

    # Exceeding maximum lot size
    with pytest.raises(InvalidQuantityError, match="exceeds maximum"):
        order_manager.validate_order_quantity(10.0, min_lot_size=0.01, max_lot_size=5.0)


def test_create_short_straddle_sell_orders(order_manager, test_instruments):
    """
    Test 3: Verify creation of short straddle SELL orders.
    - Orders are created as NautilusTrader LimitOrders with OrderSide.SELL.
    - Initial status is NOT_SUBMITTED.
    """
    call_inst, put_inst = test_instruments

    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.02,
        call_price=1250.0,
        put_price=1150.0,
    )

    assert straddle is not None
    assert straddle.status == StraddleExecutionStatus.NOT_SUBMITTED
    assert straddle.call_leg.status == LegExecutionStatus.NOT_SUBMITTED
    assert straddle.put_leg.status == LegExecutionStatus.NOT_SUBMITTED

    # Verify Call leg order
    assert straddle.call_leg.order_side == OrderSide.SELL
    assert straddle.call_leg.target_quantity == 0.02
    assert straddle.call_leg.target_price == 1250.0
    assert straddle.call_leg.nautilus_order.side == OrderSide.SELL
    assert straddle.call_leg.nautilus_order.status == OrderStatus.INITIALIZED

    # Verify Put leg order
    assert straddle.put_leg.order_side == OrderSide.SELL
    assert straddle.put_leg.target_quantity == 0.02
    assert straddle.put_leg.target_price == 1150.0
    assert straddle.put_leg.nautilus_order.side == OrderSide.SELL
    assert straddle.put_leg.nautilus_order.status == OrderStatus.INITIALIZED


def test_submit_straddle_dry_run_does_not_assume_execution(order_manager, test_instruments):
    """
    Test 4: Verify order submission in DRY_RUN mode.
    - Transitions states to SUBMITTED.
    - Crucial invariant: Submission does NOT imply execution (filled_quantity remains 0).
    """
    call_inst, put_inst = test_instruments
    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.01,
        call_price=1200.0,
        put_price=1200.0,
    )

    order_manager.submit_straddle(straddle)

    # Verify status transitions
    assert straddle.call_leg.status == LegExecutionStatus.SUBMITTED
    assert straddle.put_leg.status == LegExecutionStatus.SUBMITTED
    assert straddle.status == StraddleExecutionStatus.SUBMITTED

    # Crucial check: Zero fills recorded upon submission
    assert straddle.call_leg.filled_quantity == 0.0
    assert straddle.put_leg.filled_quantity == 0.0
    assert straddle.call_leg.is_filled is False
    assert straddle.put_leg.is_filled is False
    assert straddle.total_filled_quantity == 0.0
    assert straddle.net_premium_collected == 0.0


def test_order_acceptance_and_venue_order_id_tracking(order_manager, test_instruments):
    """
    Test 5: Verify OrderAccepted events update status to ACCEPTED and track venue_order_id.
    """
    call_inst, put_inst = test_instruments
    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.01,
        call_price=1200.0,
        put_price=1200.0,
    )
    order_manager.submit_straddle(straddle)

    # Simulate acceptance on both legs
    order_manager.simulate_acceptance(straddle.call_leg.client_order_id, venue_order_id="VENUE-CALL-001")
    order_manager.simulate_acceptance(straddle.put_leg.client_order_id, venue_order_id="VENUE-PUT-001")

    assert straddle.call_leg.status == LegExecutionStatus.ACCEPTED
    assert straddle.put_leg.status == LegExecutionStatus.ACCEPTED
    assert straddle.call_leg.venue_order_id.value == "VENUE-CALL-001"
    assert straddle.put_leg.venue_order_id.value == "VENUE-PUT-001"
    assert straddle.status == StraddleExecutionStatus.ACCEPTED


def test_order_partial_and_complete_fills_with_vwap(order_manager, test_instruments):
    """
    Test 6: Verify partial fills, complete fills, and accurate VWAP calculation.
    """
    call_inst, put_inst = test_instruments
    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.02,
        call_price=1200.0,
        put_price=1100.0,
    )
    order_manager.submit_straddle(straddle)
    order_manager.simulate_acceptance(straddle.call_leg.client_order_id)
    order_manager.simulate_acceptance(straddle.put_leg.client_order_id)

    # Call Leg: First partial fill of 0.01 @ $1200.0
    order_manager.simulate_fill(
        straddle.call_leg.client_order_id,
        fill_quantity=0.01,
        fill_price=1200.0,
        trade_id="TR-C-1",
    )
    assert straddle.call_leg.status == LegExecutionStatus.PARTIALLY_FILLED
    assert straddle.call_leg.filled_quantity == 0.01
    assert straddle.call_leg.leaves_quantity == 0.01
    assert straddle.call_leg.average_price == 1200.0

    # Call Leg: Second partial fill of 0.01 @ $1300.0 -> completing full 0.02
    order_manager.simulate_fill(
        straddle.call_leg.client_order_id,
        fill_quantity=0.01,
        fill_price=1300.0,
        trade_id="TR-C-2",
    )
    assert straddle.call_leg.status == LegExecutionStatus.FILLED
    assert straddle.call_leg.filled_quantity == 0.02
    assert straddle.call_leg.leaves_quantity == 0.0
    # VWAP = (0.01 * 1200 + 0.01 * 1300) / 0.02 = 1250.0
    assert straddle.call_leg.average_price == 1250.0

    # Put Leg: Complete fill in one trade of 0.02 @ $1100.0
    order_manager.simulate_fill(
        straddle.put_leg.client_order_id,
        fill_quantity=0.02,
        fill_price=1100.0,
        trade_id="TR-P-1",
    )
    assert straddle.put_leg.status == LegExecutionStatus.FILLED
    assert straddle.put_leg.filled_quantity == 0.02
    assert straddle.put_leg.average_price == 1100.0

    # Both legs filled -> Straddle is COMPLETED
    assert straddle.status == StraddleExecutionStatus.COMPLETED
    assert straddle.is_fully_filled is True
    assert straddle.total_filled_quantity == 0.04
    # Premium = (0.02 * 1250.0) + (0.02 * 1100.0) = 25.0 + 22.0 = 47.0 USDT
    assert straddle.net_premium_collected == 47.0


def test_order_rejection_tracking(order_manager, test_instruments):
    """
    Test 7: Verify OrderRejected updates status to REJECTED and records reason.
    """
    call_inst, put_inst = test_instruments
    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.01,
        call_price=1200.0,
        put_price=1200.0,
    )
    order_manager.submit_straddle(straddle)

    # Rejection on Put leg due to margin
    order_manager.simulate_rejection(
        straddle.put_leg.client_order_id,
        reason="Account has insufficient margin for option short",
    )

    assert straddle.put_leg.status == LegExecutionStatus.REJECTED
    assert straddle.put_leg.rejection_reason == "Account has insufficient margin for option short"
    assert straddle.status == StraddleExecutionStatus.REJECTED


def test_order_cancellation_tracking(order_manager, test_instruments):
    """
    Test 8: Verify cancellation transitions leg and straddle states to CANCELED.
    """
    call_inst, put_inst = test_instruments
    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.01,
        call_price=1200.0,
        put_price=1200.0,
    )
    order_manager.submit_straddle(straddle)

    order_manager.cancel_straddle(straddle)

    assert straddle.call_leg.status == LegExecutionStatus.CANCELED
    assert straddle.put_leg.status == LegExecutionStatus.CANCELED
    assert straddle.status == StraddleExecutionStatus.CANCELED


def test_prevent_duplicate_orders(order_manager, test_instruments):
    """
    Test 9: Verify prevention of duplicate orders and duplicate straddle submissions.
    """
    call_inst, put_inst = test_instruments
    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.01,
        call_price=1200.0,
        put_price=1200.0,
    )

    # Submitting same straddle twice raises DuplicateOrderError
    order_manager.submit_straddle(straddle)
    with pytest.raises(DuplicateOrderError, match="has already been submitted"):
        order_manager.submit_straddle(straddle)

    # Attempting to create a second straddle while first is active raises DuplicateOrderError
    with pytest.raises(DuplicateOrderError, match="Active straddle"):
        order_manager.create_straddle_sell_orders(
            call_instrument=call_inst,
            put_instrument=put_inst,
            quantity=0.01,
        )


def test_unmatched_leg_scenario_call_filled_put_pending(order_manager, test_instruments):
    """
    Test 10 (CRITICAL REQUIREMENT):
    Scenario: Call = Fully Filled, Put = Pending.
    The system must recognize that only one leg has filled (ONE_LEG_FILLED).
    """
    call_inst, put_inst = test_instruments
    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.01,
        call_price=1200.0,
        put_price=1100.0,
    )
    order_manager.submit_straddle(straddle)
    order_manager.simulate_acceptance(straddle.call_leg.client_order_id)
    order_manager.simulate_acceptance(straddle.put_leg.client_order_id)

    # Call leg fully fills
    order_manager.simulate_fill(
        straddle.call_leg.client_order_id,
        fill_quantity=0.01,
        fill_price=1200.0,
    )

    # Put leg remains in ACCEPTED (pending) status
    assert straddle.call_leg.status == LegExecutionStatus.FILLED
    assert straddle.put_leg.status == LegExecutionStatus.ACCEPTED

    # System must detect ONE_LEG_FILLED
    assert straddle.status == StraddleExecutionStatus.ONE_LEG_FILLED
    assert straddle.is_one_leg_filled is True

    # Identify which leg is filled and which is pending
    filled_leg, pending_leg = order_manager.check_unmatched_leg(straddle)
    assert filled_leg.leg_type == OptionLegType.CALL
    assert filled_leg.filled_quantity == 0.01
    assert pending_leg.leg_type == OptionLegType.PUT
    assert pending_leg.status == LegExecutionStatus.ACCEPTED


def test_unmatched_leg_policy_wait(order_manager, test_instruments):
    """
    Test 11: Unmatched-leg policy: WAIT.
    System maintains wait state for pending leg without cancellation or close.
    """
    call_inst, put_inst = test_instruments
    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.01,
        call_price=1200.0,
        put_price=1100.0,
        unmatched_policy=UnmatchedLegPolicy.WAIT,
    )
    order_manager.submit_straddle(straddle)
    order_manager.simulate_acceptance(straddle.call_leg.client_order_id)
    order_manager.simulate_acceptance(straddle.put_leg.client_order_id)

    # Fill Call leg only
    order_manager.simulate_fill(straddle.call_leg.client_order_id, fill_quantity=0.01, fill_price=1200.0)

    # Resolve under WAIT policy
    result = order_manager.resolve_unmatched_leg(straddle)
    assert result["policy"] == "WAIT"
    assert "AWAITING_SECOND_LEG" in result["actions_taken"]
    assert straddle.put_leg.status == LegExecutionStatus.ACCEPTED  # Not canceled


def test_unmatched_leg_policy_cancel_remaining(order_manager, test_instruments):
    """
    Test 12: Unmatched-leg policy: CANCEL_REMAINING.
    Cancels outstanding Put order; does not submit buyback for Call.
    """
    call_inst, put_inst = test_instruments
    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.01,
        call_price=1200.0,
        put_price=1100.0,
        unmatched_policy=UnmatchedLegPolicy.CANCEL_REMAINING,
    )
    order_manager.submit_straddle(straddle)
    order_manager.simulate_acceptance(straddle.call_leg.client_order_id)
    order_manager.simulate_acceptance(straddle.put_leg.client_order_id)

    # Fill Call leg only
    order_manager.simulate_fill(straddle.call_leg.client_order_id, fill_quantity=0.01, fill_price=1200.0)

    # Resolve under CANCEL_REMAINING policy
    result = order_manager.resolve_unmatched_leg(straddle)
    assert result["policy"] == "CANCEL_REMAINING"
    assert "CANCELED_PUT_LEG" in result["actions_taken"]
    assert straddle.put_leg.status == LegExecutionStatus.CANCELED
    assert straddle.closing_call_order is None  # No closing order created


def test_unmatched_leg_policy_cancel_and_close(order_manager, test_instruments):
    """
    Test 13: Unmatched-leg policy: CANCEL_AND_CLOSE.
    1. Cancels the outstanding pending Put order.
    2. Submits an offsetting BUY order for the filled Call leg to buy back and neutralize risk.
    """
    call_inst, put_inst = test_instruments
    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.01,
        call_price=1200.0,
        put_price=1100.0,
        unmatched_policy=UnmatchedLegPolicy.CANCEL_AND_CLOSE,
    )
    order_manager.submit_straddle(straddle)
    order_manager.simulate_acceptance(straddle.call_leg.client_order_id)
    order_manager.simulate_acceptance(straddle.put_leg.client_order_id)

    # Fill Call leg only
    order_manager.simulate_fill(straddle.call_leg.client_order_id, fill_quantity=0.01, fill_price=1200.0)

    # Resolve under CANCEL_AND_CLOSE policy
    result = order_manager.resolve_unmatched_leg(straddle)
    assert result["policy"] == "CANCEL_AND_CLOSE"
    assert "CANCELED_PUT_LEG" in result["actions_taken"]
    assert "SUBMITTED_BUYBACK_FOR_CALL" in result["actions_taken"]

    # Pending Put leg canceled
    assert straddle.put_leg.status == LegExecutionStatus.CANCELED

    # Offsetting closing BUY order created and submitted for Call
    assert straddle.closing_call_order is not None
    assert straddle.closing_call_order.order_side == OrderSide.BUY
    assert straddle.closing_call_order.target_quantity == 0.01
    assert straddle.closing_call_order.status == LegExecutionStatus.SUBMITTED
    assert straddle.status == StraddleExecutionStatus.RESOLVING_UNMATCHED


def test_independent_leg_tracking(order_manager, test_instruments):
    """
    Test 14: Verify each option leg is tracked independently.
    Call and Put have separate trackers, order IDs, and states.
    """
    call_inst, put_inst = test_instruments
    straddle = order_manager.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=0.01,
        call_price=1200.0,
        put_price=1100.0,
    )
    order_manager.submit_straddle(straddle)

    # Separate trackers
    assert straddle.call_leg is not straddle.put_leg
    assert straddle.call_leg.client_order_id != straddle.put_leg.client_order_id
    assert straddle.call_leg.leg_type == OptionLegType.CALL
    assert straddle.put_leg.leg_type == OptionLegType.PUT

    # Accept Call leg only
    order_manager.simulate_acceptance(straddle.call_leg.client_order_id)
    assert straddle.call_leg.status == LegExecutionStatus.ACCEPTED
    assert straddle.put_leg.status == LegExecutionStatus.SUBMITTED
