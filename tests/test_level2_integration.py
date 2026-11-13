"""
Unit and Integration Tests for Complete Level 2 Strategy Integration (Part 13).

Verifies the 8 deterministic simulation scenarios and the complete 13-requirement
lifecycle for the BTC options short straddle strategy:
1. Start strategy.
2. Connect to Bybit Testnet.
3. Select ATM Call and Put.
4. Submit entry orders.
5. Confirm actual entry fills.
6. Calculate combined entry premium.
7. Monitor both ask-side prices.
8. Calculate combined buyback value.
9. Trigger 1% Stop-Loss.
10. Trigger 2% Take-Profit.
11. Submit closing orders.
12. Handle partial executions.
13. Confirm both positions flat.
"""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from nautilus_trader.common.component import TestClock
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity

from strategy.level2_simulation import (
    create_sim_straddle_pair,
    run_all_level2_simulations,
    simulate_call_only_fill,
    simulate_duplicate_trigger_scenario,
    simulate_exit_rejection_scenario,
    simulate_normal_market_movement,
    simulate_partial_exit_scenario,
    simulate_put_only_fill,
    simulate_stop_loss_scenario,
    simulate_take_profit_scenario,
)
from strategy.order_manager import StraddleExitState
from strategy.risk_manager import RiskManagerState, RiskTriggerType
from strategy.straddle_strategy import (
    BtcShortStraddleStrategy,
    BtcShortStraddleStrategyConfig,
    StraddleStrategyState,
)


# -----------------------------------------------------------------------------
# 8 Deterministic Simulation Unit Tests
# -----------------------------------------------------------------------------


def test_simulation_scenario_1_normal_market():
    """Verify Scenario 1: Normal market movement within thresholds."""
    result = simulate_normal_market_movement()
    assert result.passed is True, f"Scenario 1 failed: {result.error_message}"
    assert result.details["current_buyback"] <= result.details["sl_threshold"]
    assert result.details["current_buyback"] >= result.details["tp_threshold"]


def test_simulation_scenario_2_stop_loss():
    """Verify Scenario 2: Stop-Loss breach, automated exit, and flat position."""
    result = simulate_stop_loss_scenario()
    assert result.passed is True, f"Scenario 2 failed: {result.error_message}"
    assert result.details["is_flat"] is True
    assert result.details["breach_buyback"] >= result.details["sl_threshold"]


def test_simulation_scenario_3_take_profit():
    """Verify Scenario 3: Take-Profit reach, automated exit, and flat position."""
    result = simulate_take_profit_scenario()
    assert result.passed is True, f"Scenario 3 failed: {result.error_message}"
    assert result.details["is_flat"] is True
    assert result.details["buyback_value"] <= result.details["tp_threshold"]
    assert result.details["realized_pnl"] > 0.0


def test_simulation_scenario_4_call_only_fill():
    """Verify Scenario 4: Call-only entry fill unmatched leg resolution."""
    result = simulate_call_only_fill()
    assert result.passed is True, f"Scenario 4 failed: {result.error_message}"
    assert result.details["is_flat"] is True


def test_simulation_scenario_5_put_only_fill():
    """Verify Scenario 5: Put-only entry fill unmatched leg resolution."""
    result = simulate_put_only_fill()
    assert result.passed is True, f"Scenario 5 failed: {result.error_message}"
    assert result.details["is_flat"] is True


def test_simulation_scenario_6_partial_exit_leaves():
    """Verify Scenario 6: Partial exit fill and leaves reconciliation."""
    result = simulate_partial_exit_scenario()
    assert result.passed is True, f"Scenario 6 failed: {result.error_message}"
    assert result.details["is_flat"] is True
    assert result.details["closed_put_qty"] == result.details["initial_put_close"]


def test_simulation_scenario_7_exit_rejection():
    """Verify Scenario 7: Exit order rejected by venue."""
    result = simulate_exit_rejection_scenario()
    assert result.passed is True, f"Scenario 7 failed: {result.error_message}"
    assert result.details["state"] == "EXIT_FAILED"
    assert result.details["is_verified_closed"] is False


def test_simulation_scenario_8_duplicate_trigger():
    """Verify Scenario 8: Duplicate trigger prevention and idempotency."""
    result = simulate_duplicate_trigger_scenario()
    assert result.passed is True, f"Scenario 8 failed: {result.error_message}"
    assert result.details["is_idempotent"] is True
    assert result.details["call_orders_emitted"] == 1
    assert result.details["put_orders_emitted"] == 1


def test_run_all_level2_simulations_suite():
    """Verify master simulation suite executes all 8 scenarios successfully."""
    results = run_all_level2_simulations()
    assert len(results) == 8
    for res in results:
        assert res.passed is True, f"Failed in suite: {res.scenario_name} - {res.error_message}"


# -----------------------------------------------------------------------------
# End-to-End Level 2 Strategy Lifecycle Test
# -----------------------------------------------------------------------------


def test_end_to_end_level2_strategy_complete_lifecycle():
    """
    Comprehensive verification of all 13 Level 2 requirements inside BtcShortStraddleStrategy:
    1. Start strategy.
    2. Connect (DRY_RUN mode).
    3. Select ATM Call and Put.
    4. Submit entry orders.
    5. Confirm actual entry fills.
    6. Calculate combined entry premium.
    7. Monitor both ask-side prices.
    8. Calculate combined buyback value.
    9. Trigger 1% Stop-Loss.
    10. (Tested independently in Scenario 3 for 2% Take-Profit).
    11. Submit closing orders.
    12. Handle partial executions.
    13. Confirm both positions flat.
    """
    call_inst, put_inst, chain = create_sim_straddle_pair(strike=70000.0)
    config = BtcShortStraddleStrategyConfig(
        order_quantity=0.05,
        dry_run=True,
        stop_loss_percent=1.0,
        take_profit_percent=2.0,
        auto_close_on_risk=True,
    )
    strategy = BtcShortStraddleStrategy(config=config)

    # 1. Start strategy
    strategy.on_start()
    assert strategy.strategy_state == StraddleStrategyState.AWAITING_INSTRUMENTS

    # Ingest options chain
    strategy.set_options_chain(chain)

    # Ingest BTC reference price ($70,000)
    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    underlying_id = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
    strategy.on_quote_tick(
        QuoteTick(underlying_id, Price.from_str("69990.0"), Price.from_str("70010.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns)
    )

    # 3. ATM Call and Put selected
    assert strategy.selected_straddle is not None
    assert strategy.selected_straddle.strike_price == 70000.0
    assert strategy.call_instrument_id == call_inst.id
    assert strategy.put_instrument_id == put_inst.id
    assert strategy.strategy_state == StraddleStrategyState.VALIDATING_QUOTES

    # Ingest initial option quotes to trigger entry
    strategy.on_quote_tick(
        QuoteTick(call_inst.id, Price.from_str("2480.0"), Price.from_str("2500.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns)
    )
    strategy.on_quote_tick(
        QuoteTick(put_inst.id, Price.from_str("2480.0"), Price.from_str("2500.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns)
    )

    # 4. Entry orders submitted
    assert strategy.orders_submitted is True
    assert strategy.strategy_state == StraddleStrategyState.AWAITING_FILLS
    straddle = strategy.straddle_order
    assert straddle is not None

    call_cid = straddle.call_leg.client_order_id
    put_cid = straddle.put_leg.client_order_id

    # 5. Confirm actual entry fills
    strategy.order_manager.simulate_acceptance(call_cid)
    strategy.order_manager.simulate_acceptance(put_cid)
    fill_c = strategy.order_manager.simulate_fill(call_cid, fill_quantity=0.05, fill_price=2480.0)
    fill_p = strategy.order_manager.simulate_fill(put_cid, fill_quantity=0.05, fill_price=2480.0)

    strategy.on_order_filled(fill_c)
    strategy.on_order_filled(fill_p)

    # 6. Calculate combined entry premium & thresholds
    assert strategy.strategy_state == StraddleStrategyState.POSITION_ACTIVE
    assert strategy.risk_manager.is_armed is True
    assert strategy.risk_manager.entry_premium == Decimal("248.00")
    # SL Threshold (+1.0%) = 248.00 * 1.01 = $250.48
    assert strategy.risk_manager.sl_threshold == Decimal("250.48")
    # TP Threshold (-2.0%) = 248.00 * 0.98 = $243.04
    assert strategy.risk_manager.tp_threshold == Decimal("243.04")

    # 7 & 8. Monitor ask-side prices and calculate buyback value:
    # Safe quote tick: Call ask $2485, Put ask $2485 -> Buyback = $248.50
    now_ns_2 = now_ns + int(1e9)
    strategy.on_quote_tick(
        QuoteTick(call_inst.id, Price.from_str("2460.0"), Price.from_str("2485.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns_2, now_ns_2)
    )
    strategy.on_quote_tick(
        QuoteTick(put_inst.id, Price.from_str("2460.0"), Price.from_str("2485.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns_2, now_ns_2)
    )
    assert strategy.risk_manager.latest_buyback_value == Decimal("248.50")
    assert strategy.strategy_state == StraddleStrategyState.POSITION_ACTIVE

    # 9. Trigger 1% Stop-Loss:
    # Breaching quote tick: Call ask $2520, Put ask $2510 -> Buyback = (0.05*2520) + (0.05*2510) = $251.50 >= $250.48
    now_ns_3 = now_ns + int(2e9)
    strategy.on_quote_tick(
        QuoteTick(call_inst.id, Price.from_str("2500.0"), Price.from_str("2520.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns_3, now_ns_3)
    )
    strategy.on_quote_tick(
        QuoteTick(put_inst.id, Price.from_str("2490.0"), Price.from_str("2510.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns_3, now_ns_3)
    )

    # 11. Closing BUY orders submitted automatically
    assert strategy.strategy_state == StraddleStrategyState.CLOSING_ORDERS_SUBMITTED
    active_exit = strategy.order_manager.active_exit
    assert active_exit is not None
    assert active_exit.trigger_type == "STOP_LOSS"
    assert len(active_exit.call_closing_order_ids) == 1
    assert len(active_exit.put_closing_order_ids) == 1

    exit_c_cid = active_exit.call_closing_order_ids[0]
    exit_p_cid = active_exit.put_closing_order_ids[0]

    # 12. Handle partial executions & acceptances
    strategy.order_manager.simulate_exit_acceptance(exit_c_cid)
    strategy.order_manager.simulate_exit_acceptance(exit_p_cid)

    # Partial execution: Call fills first
    exit_fill_c = strategy.order_manager.simulate_exit_fill(exit_c_cid, fill_quantity=0.05, fill_price=2520.0)
    strategy.on_order_filled(exit_fill_c)
    assert active_exit.state == StraddleExitState.EXIT_PARTIALLY_FILLED
    assert strategy.position_tracker.is_flat is False

    # Put fills second
    exit_fill_p = strategy.order_manager.simulate_exit_fill(exit_p_cid, fill_quantity=0.05, fill_price=2510.0)
    strategy.on_order_filled(exit_fill_p)

    # 13. Confirm both positions flat & strategy complete
    assert active_exit.state == StraddleExitState.EXIT_COMPLETED
    assert strategy.position_tracker.is_flat is True
    assert strategy.strategy_state == StraddleStrategyState.POSITION_CLOSED
    assert strategy.risk_manager.state == RiskManagerState.RESOLVED
