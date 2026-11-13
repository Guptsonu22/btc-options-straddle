"""
Unit tests for the Position Tracking & Reconciliation Engine (Part 9).

Verifies:
1. Straddle instrument pair registration.
2. Short position opening on SELL fill (quantity, entry VWAP, side).
3. Mark-to-market valuation strictly using the ASK price (cost to buy back short option).
4. Accurate unrealized PnL calculation on market movements.
5. Closing buyback on BUY fill and realized PnL calculation.
6. Straddle balance monitoring (Call Qty vs Put Qty, delta imbalance).
7. Native NautilusTrader Position event handling (PositionOpened, PositionClosed).
8. Position reconciliation against NautilusTrader positions.
"""

from datetime import datetime, timezone
import pytest

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.events.position import (
    PositionClosed,
    PositionOpened,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    InstrumentId,
    PositionId,
    StrategyId,
    Symbol,
    TradeId,
    TraderId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.core.uuid import UUID4

from strategy.position_tracker import (
    LegPositionStatus,
    OptionLegPosition,
    PositionTracker,
    StraddlePosition,
)


@pytest.fixture
def position_tracker():
    """Provides a fresh PositionTracker instance."""
    return PositionTracker()


@pytest.fixture
def test_instruments():
    """Provides sample Call and Put InstrumentIds."""
    call_id = InstrumentId(Symbol("BTC-TEST-70000-C-USDT"), Venue("BYBIT"))
    put_id = InstrumentId(Symbol("BTC-TEST-70000-P-USDT"), Venue("BYBIT"))
    return call_id, put_id


def test_register_straddle_pair(position_tracker, test_instruments):
    """Verify straddle pair registration creates initial FLAT positions."""
    call_id, put_id = test_instruments
    straddle = position_tracker.register_straddle_pair(call_id, put_id, strike_price=70000.0)

    assert straddle is not None
    assert straddle.call_position.instrument_id == call_id
    assert straddle.put_position.instrument_id == put_id
    assert straddle.call_position.side == LegPositionStatus.FLAT
    assert straddle.put_position.side == LegPositionStatus.FLAT
    assert straddle.total_contracts_open == 0.0
    assert straddle.is_balanced is True


def test_short_entry_fill_and_vwap(position_tracker, test_instruments):
    """
    Verify opening short positions via SELL fills:
    - Increments quantity.
    - Transitions side to SHORT.
    - Computes volume-weighted average entry price (VWAP).
    """
    call_id, put_id = test_instruments
    straddle = position_tracker.register_straddle_pair(call_id, put_id, strike_price=70000.0)

    # First Call SELL fill: 0.01 @ $1,200.00
    position_tracker.apply_fill(call_id, side="SELL", quantity=0.01, price=1200.0)
    call_pos = position_tracker.get_leg_position(call_id)
    assert call_pos.side == LegPositionStatus.SHORT
    assert call_pos.quantity == 0.01
    assert call_pos.entry_vwap == 1200.0
    assert call_pos.total_entry_premium == 12.0

    # Second Call SELL fill: 0.01 @ $1,300.00 -> VWAP = $1,250.00
    position_tracker.apply_fill(call_id, side="SELL", quantity=0.01, price=1300.0)
    assert call_pos.quantity == 0.02
    assert call_pos.entry_vwap == 1250.0
    assert call_pos.total_entry_premium == 25.0

    # Put SELL fill: 0.02 @ $1,100.00
    position_tracker.apply_fill(put_id, side="SELL", quantity=0.02, price=1100.0)
    put_pos = position_tracker.get_leg_position(put_id)
    assert put_pos.side == LegPositionStatus.SHORT
    assert put_pos.quantity == 0.02
    assert put_pos.entry_vwap == 1100.0
    assert put_pos.total_entry_premium == 22.0

    # Straddle aggregate checks
    assert straddle.is_balanced is True
    assert straddle.total_contracts_open == 0.04
    assert straddle.total_entry_premium == 47.0


def test_mark_to_market_valuation_using_ask_buyback_price(position_tracker, test_instruments):
    """
    Verify mark-to-market valuation strictly uses the ASK price (cost to buy back).
    Short PnL = (Entry Price - Ask Price) * Quantity.
    """
    call_id, put_id = test_instruments
    straddle = position_tracker.register_straddle_pair(call_id, put_id, strike_price=70000.0)

    # Open short Call: 0.01 @ $1,200.00
    position_tracker.apply_fill(call_id, side="SELL", quantity=0.01, price=1200.0)

    # Market moves favorably: Option decays to Bid=$1,000, Ask=$1,050
    # Strict buyback valuation uses ASK ($1,050), not Bid or Mid
    position_tracker.update_leg_quote(call_id, bid=1000.0, ask=1050.0)

    call_pos = position_tracker.get_leg_position(call_id)
    assert call_pos.current_mark_price == 1050.0
    # Expected Unrealized PnL: (1200 - 1050) * 0.01 = +$1.50
    assert call_pos.unrealized_pnl == 1.50

    # Market moves unfavorably: Call surges to Bid=$1,400, Ask=$1,450
    position_tracker.update_leg_quote(call_id, bid=1400.0, ask=1450.0)
    # Expected Unrealized PnL: (1200 - 1450) * 0.01 = -$2.50
    assert call_pos.unrealized_pnl == -2.50


def test_closing_buyback_and_realized_pnl(position_tracker, test_instruments):
    """
    Verify position reduction on BUY fill and accurate realized PnL.
    """
    call_id, put_id = test_instruments
    position_tracker.register_straddle_pair(call_id, put_id, strike_price=70000.0)

    # Open short 0.02 @ $1,200.00
    position_tracker.apply_fill(call_id, side="SELL", quantity=0.02, price=1200.0)

    # Buy back 0.01 @ $1,000.00 (Profit = (1200 - 1000) * 0.01 = +$2.00)
    position_tracker.apply_fill(call_id, side="BUY", quantity=0.01, price=1000.0)

    call_pos = position_tracker.get_leg_position(call_id)
    assert call_pos.quantity == 0.01
    assert call_pos.realized_pnl == 2.0
    assert call_pos.side == LegPositionStatus.SHORT

    # Buy back remaining 0.01 @ $1,100.00 (Profit = (1200 - 1100) * 0.01 = +$1.00)
    position_tracker.apply_fill(call_id, side="BUY", quantity=0.01, price=1100.0)
    assert call_pos.quantity == 0.0
    assert call_pos.side == LegPositionStatus.CLOSED
    assert call_pos.realized_pnl == 3.0


def test_straddle_imbalance_detection(position_tracker, test_instruments):
    """
    Verify detection of leg quantity imbalance (e.g. Call filled, Put flat or partial).
    """
    call_id, put_id = test_instruments
    straddle = position_tracker.register_straddle_pair(call_id, put_id, strike_price=70000.0)

    # Call fills 0.02, Put fills 0.01
    position_tracker.apply_fill(call_id, side="SELL", quantity=0.02, price=1200.0)
    position_tracker.apply_fill(put_id, side="SELL", quantity=0.01, price=1100.0)

    assert straddle.is_balanced is False
    assert straddle.leg_imbalance_quantity == 0.01  # Call is 0.01 greater than Put
    assert straddle.total_contracts_open == 0.03


def test_reconcile_with_nautilus_positions(position_tracker, test_instruments):
    """
    Verify position reconciliation detects discrepancies against NautilusTrader positions.
    """
    call_id, put_id = test_instruments
    position_tracker.register_straddle_pair(call_id, put_id, strike_price=70000.0)
    position_tracker.apply_fill(call_id, side="SELL", quantity=0.01, price=1200.0)

    # Empty Nautilus positions -> flags missing position
    report = position_tracker.reconcile_with_nautilus([])
    assert report["is_synchronized"] is False
    assert len(report["discrepancies"]) == 1
    assert "missing in Nautilus" in report["discrepancies"][0]


def test_straddle_position_sl_tp_thresholds(position_tracker, test_instruments):
    """
    Verify StraddlePosition Stop-Loss (1.01 * entry) and Take-Profit (0.98 * entry)
    properties and breach conditions.
    """
    call_id, put_id = test_instruments
    straddle = position_tracker.register_straddle_pair(call_id, put_id, strike_price=70000.0)

    # Short 0.01 Call @ $1,200.00 -> $12.00 entry premium
    # Short 0.01 Put  @ $1,100.00 -> $11.00 entry premium
    # Total Entry Premium = $23.00
    position_tracker.apply_fill(call_id, side="SELL", quantity=0.01, price=1200.0)
    position_tracker.apply_fill(put_id, side="SELL", quantity=0.01, price=1100.0)

    assert straddle.total_entry_premium == 23.0
    # SL = 23.0 * 1.01 = 23.23
    assert straddle.stop_loss_threshold == 23.23
    # TP = 23.0 * 0.98 = 22.54
    assert straddle.take_profit_threshold == 22.54

    # 1. Normal state: Buyback cost = $23.00
    position_tracker.update_leg_quote(call_id, bid=1180.0, ask=1200.0)
    position_tracker.update_leg_quote(put_id, bid=1080.0, ask=1100.0)
    assert straddle.total_buyback_cost == 23.00
    assert straddle.is_stop_loss_triggered is False
    assert straddle.is_take_profit_triggered is False

    # 2. Theta decay -> hits Take-Profit: Call ask=$1,170, Put ask=$1,080 -> $11.70 + $10.80 = $22.50 <= $22.54
    position_tracker.update_leg_quote(call_id, bid=1150.0, ask=1170.0)
    position_tracker.update_leg_quote(put_id, bid=1060.0, ask=1080.0)
    assert straddle.total_buyback_cost == 22.50
    assert straddle.is_take_profit_triggered is True
    assert straddle.is_stop_loss_triggered is False

    # 3. Adverse spike -> hits Stop-Loss: Call ask=$1,250, Put ask=$1,100 -> $12.50 + $11.00 = $23.50 >= $23.23
    position_tracker.update_leg_quote(call_id, bid=1230.0, ask=1250.0)
    position_tracker.update_leg_quote(put_id, bid=1080.0, ask=1100.0)
    assert straddle.total_buyback_cost == 23.50
    assert straddle.is_stop_loss_triggered is True
    assert straddle.is_take_profit_triggered is False

