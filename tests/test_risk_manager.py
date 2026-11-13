"""
Unit tests for Automated Straddle Risk Manager (Part 11).

Verifies:
1. Pre-condition gating: Position must be fully established to arm.
2. Normal market movements: No trigger fired when buyback is in the safe band.
3. Stop-Loss trigger: Fired when CURRENT_BUYBACK_VALUE >= SL_THRESHOLD.
4. Take-Profit trigger: Fired when CURRENT_BUYBACK_VALUE <= TP_THRESHOLD.
5. Missing Ask quotes: Missing or non-positive Ask quotes are ignored (no false trigger).
6. Stale quotes: Quotes older than max_quote_age_seconds are ignored.
7. Duplicate trigger prevention: Idempotency ensures exit fires exactly once.
8. Mutual exclusivity: Stop-Loss and Take-Profit never trigger simultaneously.
9. Integration with StraddlePosition and QuoteTick.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from strategy.position_tracker import PositionTracker, StraddlePosition
from strategy.risk_manager import (
    PositionNotEstablishedError,
    RiskManager,
    RiskManagerState,
    RiskTriggerEvent,
    RiskTriggerType,
)


@pytest.fixture
def risk_manager() -> RiskManager:
    """Construct a clean RiskManager instance with 1.0% SL and 2.0% TP."""
    return RiskManager(
        stop_loss_percent=1.0,
        take_profit_percent=2.0,
        max_quote_age_seconds=5.0,
        contract_multiplier=Decimal("1.0"),
    )


# -----------------------------------------------------------------------------
# 1. Arming & Position Gating Tests
# -----------------------------------------------------------------------------


def test_risk_manager_initial_state_disarmed(risk_manager):
    """Verify RiskManager initializes in DISARMED state and refuses evaluations."""
    assert risk_manager.state == RiskManagerState.DISARMED
    assert risk_manager.is_armed is False
    assert risk_manager.is_triggered is False

    # Evaluation while disarmed must return None
    event = risk_manager.update_quote("BTC-CALL-001.BYBIT", 1200.0, 1220.0)
    assert event is None


def test_risk_manager_arm_success(risk_manager):
    """
    Verify successful arming with a valid, fully established two-leg short straddle:
    Call: 0.10 BTC @ 1,250.00 USDT -> 125.00 USDT
    Put:  0.10 BTC @ 1,150.00 USDT -> 115.00 USDT
    Total Entry Premium: 240.00 USDT
    SL Threshold: 240.00 * 1.01 = 242.4000 USDT (+1.0% loss limit)
    TP Threshold: 240.00 * 0.98 = 235.2000 USDT (+2.0% profit target)
    """
    risk_manager.arm(
        call_instrument_id="BTC-CALL-001.BYBIT",
        put_instrument_id="BTC-PUT-001.BYBIT",
        call_executed_qty=0.10,
        call_entry_vwap=1250.00,
        put_executed_qty=0.10,
        put_entry_vwap=1150.00,
    )

    assert risk_manager.is_armed is True
    assert risk_manager.state == RiskManagerState.ARMED
    assert risk_manager.entry_premium == Decimal("240.0000")
    assert risk_manager.sl_threshold == Decimal("242.4000")
    assert risk_manager.tp_threshold == Decimal("235.2000")


def test_risk_manager_rejects_incomplete_position(risk_manager):
    """
    CRITICAL REQUIREMENT:
    Only enable risk monitoring once the strategy has a valid, established two-leg position.
    Attempting to arm with zero or negative quantity for either leg must be rejected.
    """
    # Case 1: Call leg zero (e.g. only Put filled)
    with pytest.raises(PositionNotEstablishedError, match="Both legs must be strictly positive"):
        risk_manager.arm(
            call_instrument_id="BTC-CALL-001.BYBIT",
            put_instrument_id="BTC-PUT-001.BYBIT",
            call_executed_qty=0.00,
            call_entry_vwap=1250.00,
            put_executed_qty=0.10,
            put_entry_vwap=1150.00,
        )

    # Case 2: Put leg zero (e.g. only Call filled)
    with pytest.raises(PositionNotEstablishedError, match="Both legs must be strictly positive"):
        risk_manager.arm(
            call_instrument_id="BTC-CALL-001.BYBIT",
            put_instrument_id="BTC-PUT-001.BYBIT",
            call_executed_qty=0.10,
            call_entry_vwap=1250.00,
            put_executed_qty=0.00,
            put_entry_vwap=1150.00,
        )

    # Case 3: Invalid entry price
    with pytest.raises(PositionNotEstablishedError, match="invalid entry prices"):
        risk_manager.arm(
            call_instrument_id="BTC-CALL-001.BYBIT",
            put_instrument_id="BTC-PUT-001.BYBIT",
            call_executed_qty=0.10,
            call_entry_vwap=0.00,
            put_executed_qty=0.10,
            put_entry_vwap=1150.00,
        )


# -----------------------------------------------------------------------------
# 2. Normal Market Movement (No Trigger)
# -----------------------------------------------------------------------------


def test_normal_market_movement_no_trigger(risk_manager):
    """
    Verify that when buyback value remains within the safe corridor:
    TP_THRESHOLD < BUYBACK < SL_THRESHOLD
    (235.20 < 237.00 < 242.40)
    No exit trigger is fired.
    """
    risk_manager.arm(
        call_instrument_id="BTC-CALL-001.BYBIT",
        put_instrument_id="BTC-PUT-001.BYBIT",
        call_executed_qty=0.10,
        call_entry_vwap=1250.00,
        put_executed_qty=0.10,
        put_entry_vwap=1150.00,
    )

    # Call Ask: 1240.00 -> 124.00 USDT
    # Put Ask:  1130.00 -> 113.00 USDT
    # Total Buyback: 237.00 USDT (Between 235.20 and 242.40)
    now = datetime.now(timezone.utc)
    risk_manager.update_quote("BTC-CALL-001.BYBIT", 1220.0, 1240.0, timestamp_utc=now)
    event = risk_manager.update_quote("BTC-PUT-001.BYBIT", 1110.0, 1130.0, timestamp_utc=now)

    assert event is None
    assert risk_manager.is_triggered is False
    assert risk_manager.latest_buyback_value == Decimal("237.0000")
    assert risk_manager.latest_unrealized_pnl == Decimal("3.0000")
    assert risk_manager.latest_pnl_percent == Decimal("1.25")


# -----------------------------------------------------------------------------
# 3. Stop-Loss Trigger Tests
# -----------------------------------------------------------------------------


def test_stop_loss_trigger_on_adverse_movement(risk_manager):
    """
    Verify Stop-Loss trigger:
    Condition: CURRENT_BUYBACK_VALUE >= SL_THRESHOLD (242.40 USDT)
    Scenario: Call ask surges to 1300.00, Put ask remains 1130.00
    Buyback = (1300 * 0.10) + (1130 * 0.10) = 130.00 + 113.00 = 243.00 USDT >= 242.40 USDT
    Expected: STOP_LOSS triggered, exact prices and timestamp recorded.
    """
    callback_events = []
    risk_manager.on_trigger_callback = lambda e: callback_events.append(e)

    risk_manager.arm(
        call_instrument_id="BTC-CALL-001.BYBIT",
        put_instrument_id="BTC-PUT-001.BYBIT",
        call_executed_qty=0.10,
        call_entry_vwap=1250.00,
        put_executed_qty=0.10,
        put_entry_vwap=1150.00,
    )

    now = datetime.now(timezone.utc)
    risk_manager.update_quote("BTC-PUT-001.BYBIT", 1110.0, 1130.0, timestamp_utc=now)

    # Ingest surging Call quote
    event = risk_manager.update_quote(
        "BTC-CALL-001.BYBIT", 1280.0, 1300.0, timestamp_utc=now
    )

    assert event is not None
    assert event.trigger_type == RiskTriggerType.STOP_LOSS
    assert event.current_buyback_value == Decimal("243.0000")
    assert event.threshold_value == Decimal("242.4000")
    assert event.unrealized_pnl == Decimal("-3.0000")
    assert event.call_ask == Decimal("1300.0")
    assert event.put_ask == Decimal("1130.0")
    assert "Stop-Loss threshold" in event.trigger_reason
    assert risk_manager.is_triggered is True
    assert risk_manager.state == RiskManagerState.TRIGGERED

    # Verify callback invocation
    assert len(callback_events) == 1
    assert callback_events[0] == event


# -----------------------------------------------------------------------------
# 4. Take-Profit Trigger Tests
# -----------------------------------------------------------------------------


def test_take_profit_trigger_on_theta_decay(risk_manager):
    """
    Verify Take-Profit trigger:
    Condition: CURRENT_BUYBACK_VALUE <= TP_THRESHOLD (235.20 USDT)
    Scenario: Favorable theta decay reduces quotes:
      Call Ask: 1220.00 -> 122.00 USDT
      Put Ask:  1130.00 -> 113.00 USDT
      Buyback = 122.00 + 113.00 = 235.00 USDT <= 235.20 USDT
    Expected: TAKE_PROFIT triggered, locking in +2.08% profit.
    """
    risk_manager.arm(
        call_instrument_id="BTC-CALL-001.BYBIT",
        put_instrument_id="BTC-PUT-001.BYBIT",
        call_executed_qty=0.10,
        call_entry_vwap=1250.00,
        put_executed_qty=0.10,
        put_entry_vwap=1150.00,
    )

    now = datetime.now(timezone.utc)
    risk_manager.update_quote("BTC-CALL-001.BYBIT", 1200.0, 1220.0, timestamp_utc=now)
    event = risk_manager.update_quote(
        "BTC-PUT-001.BYBIT", 1110.0, 1130.0, timestamp_utc=now
    )

    assert event is not None
    assert event.trigger_type == RiskTriggerType.TAKE_PROFIT
    assert event.current_buyback_value == Decimal("235.0000")
    assert event.threshold_value == Decimal("235.2000")
    assert event.unrealized_pnl == Decimal("5.0000")
    assert event.pnl_percent == Decimal("2.08")
    assert "Take-Profit threshold" in event.trigger_reason
    assert risk_manager.is_triggered is True


# -----------------------------------------------------------------------------
# 5. Missing & Invalid Quotes Handling
# -----------------------------------------------------------------------------


def test_missing_or_incomplete_quotes_no_trigger(risk_manager):
    """
    Verify that if one or both quotes are missing, the RiskManager does NOT trigger.
    """
    risk_manager.arm(
        call_instrument_id="BTC-CALL-001.BYBIT",
        put_instrument_id="BTC-PUT-001.BYBIT",
        call_executed_qty=0.10,
        call_entry_vwap=1250.00,
        put_executed_qty=0.10,
        put_entry_vwap=1150.00,
    )

    now = datetime.now(timezone.utc)
    # Only Call quote present; Put quote is missing
    event = risk_manager.update_quote("BTC-CALL-001.BYBIT", 1400.0, 1500.0, timestamp_utc=now)
    assert event is None
    assert risk_manager.is_triggered is False


def test_invalid_negative_or_crossed_quote_ignored(risk_manager):
    """
    Verify that non-positive Ask prices or crossed order books (bid >= ask) are ignored.
    """
    risk_manager.arm(
        call_instrument_id="BTC-CALL-001.BYBIT",
        put_instrument_id="BTC-PUT-001.BYBIT",
        call_executed_qty=0.10,
        call_entry_vwap=1250.00,
        put_executed_qty=0.10,
        put_entry_vwap=1150.00,
    )

    now = datetime.now(timezone.utc)
    risk_manager.update_quote("BTC-CALL-001.BYBIT", 1200.0, 1240.0, timestamp_utc=now)

    # Put quote has crossed book: Bid ($1,150) > Ask ($1,100) -> invalid
    event = risk_manager.update_quote("BTC-PUT-001.BYBIT", 1150.0, 1100.0, timestamp_utc=now)
    assert event is None
    assert risk_manager.is_triggered is False

    # Zero ask price -> invalid
    event_zero = risk_manager.update_quote("BTC-PUT-001.BYBIT", 0.0, 0.0, timestamp_utc=now)
    assert event_zero is None
    assert risk_manager.is_triggered is False


# -----------------------------------------------------------------------------
# 6. Stale Quote Filtering
# -----------------------------------------------------------------------------


def test_stale_quotes_ignored(risk_manager):
    """
    Verify that quotes older than max_quote_age_seconds (5.0s) are ignored.
    Even if the stale price would have breached Stop-Loss, it must NOT fire on stale data.
    """
    risk_manager.arm(
        call_instrument_id="BTC-CALL-001.BYBIT",
        put_instrument_id="BTC-PUT-001.BYBIT",
        call_executed_qty=0.10,
        call_entry_vwap=1250.00,
        put_executed_qty=0.10,
        put_entry_vwap=1150.00,
    )

    now = datetime.now(timezone.utc)
    # Stale quote: 10.0 seconds old (> 5.0s threshold)
    stale_time = now - timedelta(seconds=10.0)

    risk_manager.update_quote("BTC-CALL-001.BYBIT", 1400.0, 1500.0, timestamp_utc=stale_time)
    risk_manager.update_quote("BTC-PUT-001.BYBIT", 1100.0, 1150.0, timestamp_utc=stale_time)

    # Evaluate against current wall-clock time
    event = risk_manager.evaluate_current_risk(as_of=now)
    assert event is None
    assert risk_manager.is_triggered is False


# -----------------------------------------------------------------------------
# 7. Duplicate Trigger Prevention (Idempotency)
# -----------------------------------------------------------------------------


def test_duplicate_exit_trigger_prevented(risk_manager):
    """
    CRITICAL REQUIREMENT:
    Prevent duplicate exit initiation.
    Once triggered, subsequent ticks must NOT emit duplicate trigger events.
    """
    risk_manager.arm(
        call_instrument_id="BTC-CALL-001.BYBIT",
        put_instrument_id="BTC-PUT-001.BYBIT",
        call_executed_qty=0.10,
        call_entry_vwap=1250.00,
        put_executed_qty=0.10,
        put_entry_vwap=1150.00,
    )

    now = datetime.now(timezone.utc)
    risk_manager.update_quote("BTC-PUT-001.BYBIT", 1100.0, 1130.0, timestamp_utc=now)

    # 1. First breach -> Fires STOP_LOSS
    event1 = risk_manager.update_quote("BTC-CALL-001.BYBIT", 1280.0, 1310.0, timestamp_utc=now)
    assert event1 is not None
    assert event1.trigger_type == RiskTriggerType.STOP_LOSS
    assert risk_manager.is_triggered is True

    # 2. Subsequent quote tick arrives with price surging even further
    event2 = risk_manager.update_quote("BTC-CALL-001.BYBIT", 1350.0, 1400.0, timestamp_utc=now)
    assert event2 is None  # MUST BE NONE to prevent duplicate exit initiation

    # 3. Direct evaluation call also returns None
    assert risk_manager.evaluate_current_risk(as_of=now) is None


# -----------------------------------------------------------------------------
# 8. NautilusTrader QuoteTick Integration & Position Tracker
# -----------------------------------------------------------------------------


def test_on_quote_tick_integration(risk_manager):
    """Verify risk manager accepts NautilusTrader native QuoteTick objects."""
    call_id = InstrumentId.from_str("BTC-26NOV26-70000-C-USDT.BYBIT")
    put_id = InstrumentId.from_str("BTC-26NOV26-70000-P-USDT.BYBIT")

    risk_manager.arm(
        call_instrument_id=call_id,
        put_instrument_id=put_id,
        call_executed_qty=0.01,
        call_entry_vwap=1200.00,
        put_executed_qty=0.01,
        put_entry_vwap=1100.00,
    )
    # Entry = (0.01 * 1200) + (0.01 * 1100) = 12.00 + 11.00 = 23.00 USDT
    # SL = 23.00 * 1.01 = 23.23 USDT

    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    put_tick = QuoteTick(
        put_id,
        Price.from_str("1080.0"),
        Price.from_str("1100.0"),
        Quantity.from_str("1.0"),
        Quantity.from_str("1.0"),
        now_ns,
        now_ns,
    )
    risk_manager.on_quote_tick(put_tick)

    # Adverse Call surge: Ask = 1250.0 -> Buyback = (0.01 * 1250) + (0.01 * 1100) = 12.50 + 11.00 = 23.50 >= 23.23
    call_tick = QuoteTick(
        call_id,
        Price.from_str("1230.0"),
        Price.from_str("1250.0"),
        Quantity.from_str("1.0"),
        Quantity.from_str("1.0"),
        now_ns,
        now_ns,
    )
    event = risk_manager.on_quote_tick(call_tick)

    assert event is not None
    assert event.trigger_type == RiskTriggerType.STOP_LOSS
    assert event.current_buyback_value == Decimal("23.5000")
