"""
Unit and Integration Tests for Automated Exit Order Execution (Part 12).

Verifies institutional requirements for automated position closure:
1. Automated position closure when Stop-Loss (SL) or Take-Profit (TP) triggers.
2. Complete 10-step execution lifecycle:
   - Mark exit as initiated (EXIT_TRIGGERED).
   - Determine actual open quantity of Call.
   - Determine actual open quantity of Put.
   - Submit BUY orders for the short positions (OrderSide.BUY).
   - Use supported order types (MarketOrder or LimitOrder).
   - Track acknowledgements (OrderAccepted).
   - Track partial closing fills (OrderFilled).
   - Handle rejected exit orders (OrderRejected -> EXIT_FAILED).
   - Reconcile positions via PositionTracker.
   - Confirm both positions are flat before claiming closure (EXIT_COMPLETED).
3. Exit state machine transitions:
   OPEN -> EXIT_TRIGGERED -> EXIT_ORDERS_SUBMITTED -> EXIT_PARTIALLY_FILLED -> EXIT_COMPLETED / EXIT_FAILED.
4. Prevention of duplicate exit orders (idempotency).
5. Handling of the asymmetric scenario where one leg closes but the other remains open (EXIT_PARTIALLY_FILLED).
6. Overbuy prevention: Never submit more buy quantity than actual remaining short position.
7. Leaves reconciliation: If a close order partially fills, submit only the remaining required quantity.
8. Strict verification before claiming position closure: Never claim closed until verified flat.
9. End-to-end automated linkage between RiskManager and OrderManager upon SL or TP breach.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from nautilus_trader.common.component import TestClock
from nautilus_trader.model.currencies import BTC, USDT
from nautilus_trader.model.enums import (
    OptionKind,
    OrderSide,
    OrderType,
    TimeInForce,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    StrategyId,
    Symbol,
    TraderId,
    Venue,
)
from nautilus_trader.model.instruments.crypto_option import CryptoOption
from nautilus_trader.model.objects import Price, Quantity

from strategy.order_manager import (
    DuplicateExitError,
    DuplicateOrderError,
    LegExecutionStatus,
    OptionLegType,
    OrderManager,
    OrderSubmissionError,
    StraddleExecutionStatus,
    StraddleExitExecution,
    StraddleExitState,
    StraddleOrder,
)
from strategy.position_tracker import PositionTracker
from strategy.risk_manager import (
    RiskManager,
    RiskManagerState,
    RiskTriggerType,
)


# -----------------------------------------------------------------------------
# Fixtures & Test Setup
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
    """Provides paired Call and Put CryptoOptions with identical strike and expiry."""
    expiry = datetime.now(timezone.utc) + timedelta(days=2)
    call_inst = make_test_crypto_option("BTC-TEST-70000-C-USDT", OptionKind.CALL, 70000.0, expiry)
    put_inst = make_test_crypto_option("BTC-TEST-70000-P-USDT", OptionKind.PUT, 70000.0, expiry)
    return call_inst, put_inst


@pytest.fixture
def order_manager():
    """Provides fresh OrderManager in DRY_RUN mode."""
    clock = TestClock()
    return OrderManager(
        trader_id=TraderId("TRADER-TEST"),
        strategy_id=StrategyId("STRAT-TEST"),
        account_id=AccountId("BYBIT-TEST"),
        dry_run=True,
        clock=clock,
    )


@pytest.fixture
def position_tracker(test_instruments):
    """Provides a PositionTracker initialized with the test straddle pair."""
    call_inst, put_inst = test_instruments
    tracker = PositionTracker()
    tracker.register_straddle_pair(
        call_instrument_id=call_inst.id,
        put_instrument_id=put_inst.id,
        strike_price=70000.0,
    )
    return tracker


def establish_short_straddle(
    om: OrderManager,
    pt: PositionTracker,
    call_inst: CryptoOption,
    put_inst: CryptoOption,
    qty: float = 0.10,
    call_px: float = 2500.0,
    put_px: float = 2300.0,
) -> StraddleOrder:
    """Helper to establish a fully filled short straddle position."""
    om.set_position_tracker(pt)
    straddle = om.create_straddle_sell_orders(
        call_instrument=call_inst,
        put_instrument=put_inst,
        quantity=qty,
        call_price=call_px,
        put_price=put_px,
    )
    om.submit_straddle(straddle)

    # Acceptance
    om.simulate_acceptance(straddle.call_leg.client_order_id)
    om.simulate_acceptance(straddle.put_leg.client_order_id)

    # Execution fills
    om.simulate_fill(straddle.call_leg.client_order_id, fill_quantity=qty, fill_price=call_px)
    om.simulate_fill(straddle.put_leg.client_order_id, fill_quantity=qty, fill_price=put_px)

    # Register fills in position tracker
    pt.apply_fill(call_inst.id, "SELL", qty, call_px)
    pt.apply_fill(put_inst.id, "SELL", qty, put_px)

    assert straddle.status == StraddleExecutionStatus.COMPLETED
    assert not pt.is_flat
    return straddle


# -----------------------------------------------------------------------------
# Test Cases
# -----------------------------------------------------------------------------


def test_exit_state_machine_stop_loss_success(order_manager, position_tracker, test_instruments):
    """
    Test 1: Full exit on Stop-Loss trigger:
    - Initiates exit -> state EXIT_ORDERS_SUBMITTED
    - Both BUY orders accepted
    - Call BUY order fills 100% -> EXIT_PARTIALLY_FILLED
    - Put BUY order fills 100% -> EXIT_COMPLETED
    - Confirms positions are flat and exit is verified closed.
    """
    call_inst, put_inst = test_instruments
    straddle = establish_short_straddle(order_manager, position_tracker, call_inst, put_inst, qty=0.10)

    # Trigger Stop-Loss exit
    exit_exec = order_manager.initiate_straddle_exit(
        trigger_reason="Stop loss threshold $500.00 breached",
        trigger_type="STOP_LOSS",
        straddle=straddle,
        position_tracker=position_tracker,
    )

    assert exit_exec.state == StraddleExitState.EXIT_ORDERS_SUBMITTED
    assert exit_exec.trigger_type == "STOP_LOSS"
    assert exit_exec.target_call_close_quantity == 0.10
    assert exit_exec.target_put_close_quantity == 0.10
    assert len(exit_exec.call_closing_order_ids) == 1
    assert len(exit_exec.put_closing_order_ids) == 1

    call_exit_cid = exit_exec.call_closing_order_ids[0]
    put_exit_cid = exit_exec.put_closing_order_ids[0]

    # Verify orders are BUY side
    call_tracker = order_manager.get_leg_tracker(call_exit_cid)
    put_tracker = order_manager.get_leg_tracker(put_exit_cid)
    assert call_tracker.order_side == OrderSide.BUY
    assert put_tracker.order_side == OrderSide.BUY

    # Accept exit orders
    order_manager.simulate_exit_acceptance(call_exit_cid)
    order_manager.simulate_exit_acceptance(put_exit_cid)

    # 1. Fill Call leg: 0.10 BTC @ $2700
    order_manager.simulate_exit_fill(call_exit_cid, fill_quantity=0.10, fill_price=2700.0)
    assert exit_exec.is_call_closed is True
    assert exit_exec.is_put_closed is False
    assert exit_exec.state == StraddleExitState.EXIT_PARTIALLY_FILLED
    assert exit_exec.is_verified_closed(position_tracker) is False

    # 2. Fill Put leg: 0.10 BTC @ $2500
    order_manager.simulate_exit_fill(put_exit_cid, fill_quantity=0.10, fill_price=2500.0)
    assert exit_exec.is_put_closed is True
    assert exit_exec.is_fully_closed is True
    assert exit_exec.state == StraddleExitState.EXIT_COMPLETED

    # Verify positions reconciled and flat
    assert position_tracker.is_flat is True
    assert exit_exec.is_verified_closed(position_tracker) is True
    assert exit_exec.total_exit_cost == pytest.approx((0.10 * 2700.0) + (0.10 * 2500.0), abs=1e-2)
    assert exit_exec.call_exit_vwap == 2700.0
    assert exit_exec.put_exit_vwap == 2500.0


def test_exit_state_machine_take_profit_success(order_manager, position_tracker, test_instruments):
    """
    Test 2: Full exit on Take-Profit trigger:
    - Initiates exit -> state EXIT_ORDERS_SUBMITTED
    - Both BUY orders fill -> transitions to EXIT_COMPLETED
    - Positions confirmed flat.
    """
    call_inst, put_inst = test_instruments
    straddle = establish_short_straddle(order_manager, position_tracker, call_inst, put_inst, qty=0.05)

    exit_exec = order_manager.initiate_straddle_exit(
        trigger_reason="Take profit target $470.00 reached",
        trigger_type="TAKE_PROFIT",
        straddle=straddle,
        position_tracker=position_tracker,
    )

    assert exit_exec.trigger_type == "TAKE_PROFIT"
    assert exit_exec.state == StraddleExitState.EXIT_ORDERS_SUBMITTED

    call_cid = exit_exec.call_closing_order_ids[0]
    put_cid = exit_exec.put_closing_order_ids[0]

    order_manager.simulate_exit_acceptance(call_cid)
    order_manager.simulate_exit_acceptance(put_cid)

    # Fill both legs
    order_manager.simulate_exit_fill(call_cid, fill_quantity=0.05, fill_price=2200.0)
    order_manager.simulate_exit_fill(put_cid, fill_quantity=0.05, fill_price=2100.0)

    assert exit_exec.state == StraddleExitState.EXIT_COMPLETED
    assert position_tracker.is_flat is True
    assert exit_exec.is_verified_closed(position_tracker) is True


def test_asymmetric_closure_one_leg_closes_other_remains_open(
    order_manager, position_tracker, test_instruments
):
    """
    Test 3: Handle the scenario where one leg closes but the other remains open:
    - Call leg completely fills (0.10 BTC) -> is_call_closed = True
    - Put leg only partially fills (0.04 BTC out of 0.10 BTC) -> is_put_closed = False
    - System MUST maintain EXIT_PARTIALLY_FILLED state and NOT claim closure.
    """
    call_inst, put_inst = test_instruments
    straddle = establish_short_straddle(order_manager, position_tracker, call_inst, put_inst, qty=0.10)

    exit_exec = order_manager.initiate_straddle_exit(
        trigger_reason="Asymmetric test trigger",
        straddle=straddle,
        position_tracker=position_tracker,
    )

    call_cid = exit_exec.call_closing_order_ids[0]
    put_cid = exit_exec.put_closing_order_ids[0]

    order_manager.simulate_exit_acceptance(call_cid)
    order_manager.simulate_exit_acceptance(put_cid)

    # Call leg fully closes
    order_manager.simulate_exit_fill(call_cid, fill_quantity=0.10, fill_price=2600.0)
    # Put leg only partially fills 0.04 BTC
    order_manager.simulate_exit_fill(put_cid, fill_quantity=0.04, fill_price=2400.0)

    assert exit_exec.state == StraddleExitState.EXIT_PARTIALLY_FILLED
    assert exit_exec.is_call_closed is True
    assert exit_exec.is_put_closed is False
    assert exit_exec.remaining_put_quantity == 0.06
    assert position_tracker.is_flat is False
    assert exit_exec.is_verified_closed(position_tracker) is False


def test_partial_fill_resubmission_remaining_leaves(
    order_manager, position_tracker, test_instruments
):
    """
    Test 4: Leaves reconciliation:
    - If a close order partially fills, submit ONLY the remaining required quantity
      after reconciling outstanding orders and actual fills.
    - Never submit more buy quantity than remaining open short position.
    """
    call_inst, put_inst = test_instruments
    straddle = establish_short_straddle(order_manager, position_tracker, call_inst, put_inst, qty=0.10)

    exit_exec = order_manager.initiate_straddle_exit(
        trigger_reason="Test partial fill leaves retry",
        straddle=straddle,
        position_tracker=position_tracker,
    )

    put_cid = exit_exec.put_closing_order_ids[0]
    order_manager.simulate_exit_acceptance(put_cid)

    # Put order partially fills 0.04 BTC out of 0.10 BTC
    order_manager.simulate_exit_fill(put_cid, fill_quantity=0.04, fill_price=2400.0)
    assert exit_exec.remaining_put_quantity == 0.06

    # Cancel the remaining unfilled portion of the initial put order
    put_tracker = order_manager.get_leg_tracker(put_cid)
    order_manager.simulate_cancellation(put_cid)
    assert put_tracker.is_terminal is True

    # Submit remaining required quantity for Put
    retry_tracker = order_manager.submit_remaining_exit_order(
        leg_type=OptionLegType.PUT,
        exit_execution=exit_exec,
        position_tracker=position_tracker,
    )

    assert retry_tracker is not None
    # Reconciled quantity MUST be exactly 0.06 BTC (the leaves)
    assert retry_tracker.target_quantity == 0.06
    assert len(exit_exec.put_closing_order_ids) == 2

    # Accept and fill the retry order
    retry_cid = retry_tracker.client_order_id
    order_manager.simulate_exit_acceptance(retry_cid)
    order_manager.simulate_exit_fill(retry_cid, fill_quantity=0.06, fill_price=2450.0)

    assert exit_exec.closed_put_quantity == 0.10
    assert exit_exec.is_put_closed is True


def test_never_submit_more_buy_quantity_than_actual_open_short(
    order_manager, position_tracker, test_instruments
):
    """
    Test 5: Overbuy prevention:
    - Invariant: Never submit more buy quantity than actual remaining short position.
    - If entry only partially filled (e.g. Call 0.05 BTC, Put 0.02 BTC),
      exit BUY orders must be exactly 0.05 and 0.02 BTC, NOT the initial requested order size.
    """
    call_inst, put_inst = test_instruments
    order_manager.set_position_tracker(position_tracker)

    # Open only 0.05 Call and 0.02 Put in position tracker
    position_tracker.apply_fill(call_inst.id, "SELL", 0.05, 2500.0)
    position_tracker.apply_fill(put_inst.id, "SELL", 0.02, 2300.0)

    exit_exec = order_manager.initiate_straddle_exit(
        trigger_reason="Test overbuy prevention",
        call_instrument=call_inst,
        put_instrument=put_inst,
        position_tracker=position_tracker,
    )

    assert exit_exec.target_call_close_quantity == 0.05
    assert exit_exec.target_put_close_quantity == 0.02

    call_cid = exit_exec.call_closing_order_ids[0]
    put_cid = exit_exec.put_closing_order_ids[0]

    call_tracker = order_manager.get_leg_tracker(call_cid)
    put_tracker = order_manager.get_leg_tracker(put_cid)

    # Order quantities MUST strictly equal the open short positions, not more!
    assert call_tracker.target_quantity == 0.05
    assert put_tracker.target_quantity == 0.02


def test_prevent_duplicate_exit_orders(order_manager, position_tracker, test_instruments):
    """
    Test 6: Idempotency & duplicate exit order prevention:
    - Calling initiate_straddle_exit multiple times returns existing active execution.
    - Does NOT submit new duplicate orders.
    """
    call_inst, put_inst = test_instruments
    straddle = establish_short_straddle(order_manager, position_tracker, call_inst, put_inst, qty=0.10)

    exit_1 = order_manager.initiate_straddle_exit(
        trigger_reason="First SL trigger",
        straddle=straddle,
        position_tracker=position_tracker,
    )

    initial_call_orders_count = len(exit_1.call_closing_order_ids)
    initial_put_orders_count = len(exit_1.put_closing_order_ids)

    # Attempt second trigger while exit is active
    exit_2 = order_manager.initiate_straddle_exit(
        trigger_reason="Duplicate SL trigger attempt",
        straddle=straddle,
        position_tracker=position_tracker,
    )

    # Must return identical execution object without duplicate order emissions
    assert exit_1 is exit_2
    assert len(exit_1.call_closing_order_ids) == initial_call_orders_count
    assert len(exit_1.put_closing_order_ids) == initial_put_orders_count


def test_exit_order_rejected_transitions_to_failed(
    order_manager, position_tracker, test_instruments
):
    """
    Test 7: Handle rejected exit orders:
    - When exchange rejects exit BUY order, state transitions to EXIT_FAILED.
    - Records exact rejection reason.
    - Does not claim position closed.
    """
    call_inst, put_inst = test_instruments
    straddle = establish_short_straddle(order_manager, position_tracker, call_inst, put_inst, qty=0.10)

    exit_exec = order_manager.initiate_straddle_exit(
        trigger_reason="Test rejection handling",
        straddle=straddle,
        position_tracker=position_tracker,
    )

    call_cid = exit_exec.call_closing_order_ids[0]

    # Simulate venue rejection
    rejection_reason = "10006: Insufficient margin for close buyback"
    order_manager.simulate_exit_rejection(call_cid, reason=rejection_reason)

    assert exit_exec.state == StraddleExitState.EXIT_FAILED
    assert exit_exec.failure_reason == rejection_reason
    assert exit_exec.is_terminal is True
    assert exit_exec.is_verified_closed(position_tracker) is False


def test_do_not_claim_closed_until_verified(order_manager, position_tracker, test_instruments):
    """
    Test 8: Strict verification before claiming position closure:
    - Never claims position closed until:
      1. Both leg close quantities match target.
      2. PositionTracker verifies flat (quantity <= 1e-7).
    """
    call_inst, put_inst = test_instruments
    straddle = establish_short_straddle(order_manager, position_tracker, call_inst, put_inst, qty=0.10)

    exit_exec = order_manager.initiate_straddle_exit(
        trigger_reason="Verification test",
        straddle=straddle,
        position_tracker=position_tracker,
    )

    # 1. Right after submission: Not closed
    assert exit_exec.is_verified_closed(position_tracker) is False

    call_cid = exit_exec.call_closing_order_ids[0]
    put_cid = exit_exec.put_closing_order_ids[0]
    order_manager.simulate_exit_acceptance(call_cid)
    order_manager.simulate_exit_acceptance(put_cid)

    # 2. Only Call filled: Not closed
    order_manager.simulate_exit_fill(call_cid, 0.10, 2500.0)
    assert exit_exec.is_verified_closed(position_tracker) is False

    # 3. Both filled and PositionTracker verified flat: Closed!
    order_manager.simulate_exit_fill(put_cid, 0.10, 2300.0)
    assert position_tracker.is_flat is True
    assert exit_exec.is_verified_closed(position_tracker) is True
    assert exit_exec.state == StraddleExitState.EXIT_COMPLETED


def test_risk_manager_automated_exit_linkage_stop_loss(
    order_manager, position_tracker, test_instruments
):
    """
    Test 9: End-to-end RiskManager + OrderManager automated position closure on Stop-Loss:
    - Arms RiskManager with 0.05 Call @ $2500 and 0.05 Put @ $2500 (Gross credit = $250.00).
    - SL Threshold (+1.0%) = $252.50.
    - Links RiskManager to OrderManager with auto_execute_exit = True.
    - Ingests quotes with Call ask = $2600, Put ask = $2550 -> Buyback = $257.50 >= $252.50.
    - Verifies RiskManager automatically fires initiate_straddle_exit.
    - Simulates fills -> verifies RiskManager transitions to RESOLVED and position is flat.
    """
    call_inst, put_inst = test_instruments
    establish_short_straddle(
        order_manager, position_tracker, call_inst, put_inst, qty=0.05, call_px=2500.0, put_px=2500.0
    )

    risk_mgr = RiskManager(stop_loss_percent=1.0, take_profit_percent=2.0)
    risk_mgr.arm_from_straddle_position(position_tracker.get_straddle_position())
    risk_mgr.link_order_manager(order_manager, position_tracker, auto_execute_exit=True)

    assert risk_mgr.is_armed is True
    assert risk_mgr.entry_premium == Decimal("250.00")
    assert risk_mgr.sl_threshold == Decimal("252.50")

    # Ingest quote that breaches Stop-Loss: Call ask = 2600, Put ask = 2550
    now = datetime.now(timezone.utc)
    risk_mgr.update_quote(call_inst.id, bid_price=2580.0, ask_price=2600.0, timestamp_utc=now)
    trigger_event = risk_mgr.update_quote(
        put_inst.id, bid_price=2530.0, ask_price=2550.0, timestamp_utc=now
    )

    assert trigger_event is not None
    assert trigger_event.trigger_type == RiskTriggerType.STOP_LOSS
    assert risk_mgr.state == RiskManagerState.TRIGGERED

    # Verify OrderManager automatically initiated exit execution
    active_exit = risk_mgr.exit_execution
    assert active_exit is not None
    assert active_exit.state == StraddleExitState.EXIT_ORDERS_SUBMITTED
    assert active_exit.trigger_type == "STOP_LOSS"

    # Simulate venue acceptance & fills
    call_cid = active_exit.call_closing_order_ids[0]
    put_cid = active_exit.put_closing_order_ids[0]

    order_manager.simulate_exit_acceptance(call_cid)
    order_manager.simulate_exit_acceptance(put_cid)

    order_manager.simulate_exit_fill(call_cid, fill_quantity=0.05, fill_price=2600.0)
    order_manager.simulate_exit_fill(put_cid, fill_quantity=0.05, fill_price=2550.0)

    # Verify exit completion and RiskManager state resolution
    assert active_exit.state == StraddleExitState.EXIT_COMPLETED
    assert risk_mgr.state == RiskManagerState.RESOLVED
    assert position_tracker.is_flat is True


def test_risk_manager_automated_exit_linkage_take_profit(
    order_manager, position_tracker, test_instruments
):
    """
    Test 10: End-to-end RiskManager + OrderManager automated position closure on Take-Profit:
    - Arms RiskManager with 0.05 Call @ $2500 and 0.05 Put @ $2500 (Gross credit = $250.00).
    - TP Threshold (-2.0%) = $245.00.
    - Ingests quotes where Call ask = $2400, Put ask = $2400 -> Buyback = $240.00 <= $245.00.
    - Verifies automatic exit initiation, simulated fills, and resolution.
    """
    call_inst, put_inst = test_instruments
    establish_short_straddle(
        order_manager, position_tracker, call_inst, put_inst, qty=0.05, call_px=2500.0, put_px=2500.0
    )

    risk_mgr = RiskManager(stop_loss_percent=1.0, take_profit_percent=2.0)
    risk_mgr.arm_from_straddle_position(position_tracker.get_straddle_position())
    risk_mgr.link_order_manager(order_manager, position_tracker, auto_execute_exit=True)

    assert risk_mgr.tp_threshold == Decimal("245.00")

    now = datetime.now(timezone.utc)
    risk_mgr.update_quote(call_inst.id, bid_price=2380.0, ask_price=2400.0, timestamp_utc=now)
    trigger_event = risk_mgr.update_quote(
        put_inst.id, bid_price=2380.0, ask_price=2400.0, timestamp_utc=now
    )

    assert trigger_event is not None
    assert trigger_event.trigger_type == RiskTriggerType.TAKE_PROFIT

    active_exit = risk_mgr.exit_execution
    assert active_exit is not None
    assert active_exit.trigger_type == "TAKE_PROFIT"

    # Fill both legs
    call_cid = active_exit.call_closing_order_ids[0]
    put_cid = active_exit.put_closing_order_ids[0]
    order_manager.simulate_exit_acceptance(call_cid)
    order_manager.simulate_exit_acceptance(put_cid)

    order_manager.simulate_exit_fill(call_cid, fill_quantity=0.05, fill_price=2400.0)
    order_manager.simulate_exit_fill(put_cid, fill_quantity=0.05, fill_price=2400.0)

    assert active_exit.state == StraddleExitState.EXIT_COMPLETED
    assert risk_mgr.state == RiskManagerState.RESOLVED
    assert position_tracker.is_flat is True
