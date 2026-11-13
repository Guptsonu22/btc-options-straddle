"""
Deterministic Level 2 Straddle Strategy Simulations (Part 13).

Provides 8 deterministic simulation scenarios verifying Level 2 strategy integration:
1. Normal Market Movement (Quotes fluctuate within safe threshold bounds; no liquidation).
2. Stop-Loss Scenario (Combined buyback liability rises >= 101% of entry -> automated exit -> flat).
3. Take-Profit Scenario (Combined buyback liability drops <= 98% of entry -> automated exit -> flat).
4. Call-Only Fill (Unmatched leg during entry: Call filled, Put pending -> CANCEL_AND_CLOSE -> flat).
5. Put-Only Fill (Unmatched leg during entry: Put filled, Call pending -> CANCEL_AND_CLOSE -> flat).
6. Partial Exit (Leaves reconciliation: partial exit fill -> remaining quantity resubmitted -> flat).
7. Exit Rejection (Venue rejects closing BUY order -> EXIT_FAILED audit handling).
8. Duplicate Trigger (Rapid concurrent breach ticks -> idempotency prevents duplicate orders).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from nautilus_trader.common.component import TestClock
from nautilus_trader.model.currencies import BTC, USDT
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OptionKind, OrderSide
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
from strategy.order_manager import (
    LegExecutionStatus,
    OptionLegType,
    OrderManager,
    StraddleExecutionStatus,
    StraddleExitState,
    StraddleOrder,
    UnmatchedLegPolicy,
)
from strategy.position_tracker import PositionTracker
from strategy.risk_manager import (
    RiskManager,
    RiskManagerState,
    RiskTriggerType,
)
from strategy.straddle_strategy import (
    BtcShortStraddleStrategy,
    BtcShortStraddleStrategyConfig,
    StraddleStrategyState,
)

logger = logging.getLogger("btc_straddle.simulation")


# -----------------------------------------------------------------------------
# Test Instruments Helper
# -----------------------------------------------------------------------------


def create_sim_crypto_option(
    symbol_str: str,
    option_kind: OptionKind,
    strike_price_val: float,
    expiration_dt: datetime,
) -> CryptoOption:
    """Create an authentic NautilusTrader CryptoOption for deterministic simulation."""
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


def create_sim_straddle_pair(
    strike: float = 70000.0,
    dte_days: float = 2.0,
) -> Tuple[CryptoOption, CryptoOption, DiscoveredOptionsChain]:
    """Build paired Call and Put options plus chain metadata."""
    expiry = datetime.now(timezone.utc) + timedelta(days=dte_days)
    c_sym = f"BTC-TEST-{int(strike)}-C-USDT"
    p_sym = f"BTC-TEST-{int(strike)}-P-USDT"

    call_inst = create_sim_crypto_option(c_sym, OptionKind.CALL, strike, expiry)
    put_inst = create_sim_crypto_option(p_sym, OptionKind.PUT, strike, expiry)

    c_detail = OptionContractDetails(
        instrument_id=call_inst.id.value,
        underlying="BTC",
        option_type="CALL",
        strike_price=strike,
        expiration_utc=expiry,
        contract_multiplier=1.0,
        settlement_currency="USDT",
        min_order_quantity=0.01,
        quantity_increment=0.01,
        price_precision=0,
        price_increment=5.0,
        trading_status="Trading",
        is_active=True,
        raw_symbol=c_sym,
        nautilus_instrument=call_inst,
    )
    p_detail = OptionContractDetails(
        instrument_id=put_inst.id.value,
        underlying="BTC",
        option_type="PUT",
        strike_price=strike,
        expiration_utc=expiry,
        contract_multiplier=1.0,
        settlement_currency="USDT",
        min_order_quantity=0.01,
        quantity_increment=0.01,
        price_precision=0,
        price_increment=5.0,
        trading_status="Trading",
        is_active=True,
        raw_symbol=p_sym,
        nautilus_instrument=put_inst,
    )

    chain = DiscoveredOptionsChain(
        underlying="BTC",
        as_of_utc=datetime.now(timezone.utc),
        total_discovered=2,
        total_active=2,
        calls=[c_detail],
        puts=[p_detail],
        expirations=[expiry],
        strikes_by_expiry={expiry: [strike]},
        contracts_by_id={
            call_inst.id.value: c_detail,
            put_inst.id.value: p_detail,
        },
        _calls_map={(expiry, strike): c_detail},
        _puts_map={(expiry, strike): p_detail},
    )
    return call_inst, put_inst, chain


# -----------------------------------------------------------------------------
# Simulation Result Data Model
# -----------------------------------------------------------------------------


@dataclass
class SimulationResult:
    """Structured report of a deterministic simulation scenario."""

    scenario_name: str
    passed: bool
    description: str
    details: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None

    def summary(self) -> str:
        status_tag = "[PASS]" if self.passed else "[FAIL]"
        return f"{status_tag} {self.scenario_name}: {self.description}"


# -----------------------------------------------------------------------------
# Scenario 1: Normal Market Movement
# -----------------------------------------------------------------------------


def simulate_normal_market_movement() -> SimulationResult:
    """
    Scenario 1: Normal Market Movement
    - Starts strategy in DRY_RUN mode.
    - ATM selected at $70,000 strike.
    - Entry orders filled: Call 0.05 BTC @ $2500, Put 0.05 BTC @ $2300 (Entry = $240.00).
    - SL (+1%) = $242.40, TP (-2%) = $235.20.
    - Market quotes fluctuate safely: Call ask = $2490, Put ask = $2310 -> Buyback = $240.00.
    - Invariant verified: Position remains ARMED and POSITION_ACTIVE; no false liquidation.
    """
    call_inst, put_inst, chain = create_sim_straddle_pair(strike=70000.0)
    config = BtcShortStraddleStrategyConfig(
        order_quantity=0.05,
        dry_run=True,
        stop_loss_percent=1.0,
        take_profit_percent=2.0,
    )
    strategy = BtcShortStraddleStrategy(config=config)
    strategy.on_start()
    strategy.set_options_chain(chain)

    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    underlying_id = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")

    # Spot price tick -> triggers ATM selection
    strategy.on_quote_tick(
        QuoteTick(underlying_id, Price.from_str("69990.0"), Price.from_str("70010.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns)
    )

    # Initial option quotes -> triggers entry
    strategy.on_quote_tick(
        QuoteTick(call_inst.id, Price.from_str("2480.0"), Price.from_str("2500.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns)
    )
    strategy.on_quote_tick(
        QuoteTick(put_inst.id, Price.from_str("2280.0"), Price.from_str("2300.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns)
    )

    # Simulate venue acceptance & entry fills
    call_cid = strategy.straddle_order.call_leg.client_order_id
    put_cid = strategy.straddle_order.put_leg.client_order_id
    strategy.order_manager.simulate_acceptance(call_cid)
    strategy.order_manager.simulate_acceptance(put_cid)

    strategy.order_manager.simulate_fill(call_cid, fill_quantity=0.05, fill_price=2500.0)
    strategy.order_manager.simulate_fill(put_cid, fill_quantity=0.05, fill_price=2300.0)

    # Ingest fills into strategy
    strategy.position_tracker.apply_fill(call_inst.id, "SELL", 0.05, 2500.0)
    strategy.position_tracker.apply_fill(put_inst.id, "SELL", 0.05, 2300.0)
    strategy.strategy_state = StraddleStrategyState.POSITION_ACTIVE
    strategy.risk_manager.arm_from_straddle_position(strategy.position_tracker.get_straddle_position())

    # Fluctuate quotes within safe bounds: Call ask $2490, Put ask $2310
    safe_tick_c = QuoteTick(call_inst.id, Price.from_str("2470.0"), Price.from_str("2490.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns + 1000, now_ns + 1000)
    safe_tick_p = QuoteTick(put_inst.id, Price.from_str("2290.0"), Price.from_str("2310.0"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns + 1000, now_ns + 1000)

    strategy.on_quote_tick(safe_tick_c)
    strategy.on_quote_tick(safe_tick_p)

    passed = (
        strategy.strategy_state == StraddleStrategyState.POSITION_ACTIVE
        and strategy.risk_manager.is_armed is True
        and not strategy.risk_manager.is_triggered
        and (
            strategy.position_tracker.get_straddle_position() is not None
            and strategy.position_tracker.get_straddle_position().total_contracts_open == 0.10
        )
    )

    return SimulationResult(
        scenario_name="Normal Market Movement",
        passed=passed,
        description="Quotes fluctuated safely within SL/TP bounds; monitoring remained active without false triggers.",
        details={
            "entry_premium": float(strategy.risk_manager.entry_premium),
            "sl_threshold": float(strategy.risk_manager.sl_threshold),
            "tp_threshold": float(strategy.risk_manager.tp_threshold),
            "current_buyback": float(strategy.risk_manager.latest_buyback_value or 0.0),
            "state": strategy.strategy_state.value,
        },
    )


# -----------------------------------------------------------------------------
# Scenario 2: Stop-Loss Scenario
# -----------------------------------------------------------------------------


def simulate_stop_loss_scenario() -> SimulationResult:
    """
    Scenario 2: Stop-Loss Scenario
    - Entry: Call 0.05 @ $2500, Put 0.05 @ $2500 -> Entry premium = $250.00.
    - SL Threshold (+1.0%) = $252.50.
    - Market spike: Call ask $2600, Put ask $2550 -> Buyback = $257.50 >= $252.50.
    - Verifies automated exit order generation, simulated execution, and verified flat position.
    """
    call_inst, put_inst, _ = create_sim_straddle_pair(strike=70000.0)
    clock = TestClock()
    om = OrderManager(trader_id=TraderId("TRADER-SIM"), dry_run=True, clock=clock)
    pt = PositionTracker()
    pt.register_straddle_pair(call_inst.id, put_inst.id, 70000.0)
    om.set_position_tracker(pt)

    # Establish entry
    straddle = om.create_straddle_sell_orders(call_inst, put_inst, quantity=0.05, call_price=2500.0, put_price=2500.0)
    om.submit_straddle(straddle)
    om.simulate_acceptance(straddle.call_leg.client_order_id)
    om.simulate_acceptance(straddle.put_leg.client_order_id)
    om.simulate_fill(straddle.call_leg.client_order_id, fill_quantity=0.05, fill_price=2500.0)
    om.simulate_fill(straddle.put_leg.client_order_id, fill_quantity=0.05, fill_price=2500.0)
    pt.apply_fill(call_inst.id, "SELL", 0.05, 2500.0)
    pt.apply_fill(put_inst.id, "SELL", 0.05, 2500.0)

    # Setup RiskManager
    rm = RiskManager(stop_loss_percent=1.0, take_profit_percent=2.0)
    rm.arm_from_straddle_position(pt.get_straddle_position())
    rm.link_order_manager(om, pt, auto_execute_exit=True)

    # Ingest breach tick
    now = datetime.now(timezone.utc)
    rm.update_quote(call_inst.id, bid_price=2580.0, ask_price=2600.0, timestamp_utc=now)
    event = rm.update_quote(put_inst.id, bid_price=2530.0, ask_price=2550.0, timestamp_utc=now)

    assert event is not None and event.trigger_type == RiskTriggerType.STOP_LOSS
    active_exit = om.active_exit
    assert active_exit is not None

    # Complete exit executions
    call_exit_cid = active_exit.call_closing_order_ids[0]
    put_exit_cid = active_exit.put_closing_order_ids[0]
    om.simulate_exit_acceptance(call_exit_cid)
    om.simulate_exit_acceptance(put_exit_cid)
    om.simulate_exit_fill(call_exit_cid, fill_quantity=0.05, fill_price=2600.0)
    om.simulate_exit_fill(put_exit_cid, fill_quantity=0.05, fill_price=2550.0)

    passed = (
        active_exit.state == StraddleExitState.EXIT_COMPLETED
        and rm.state == RiskManagerState.RESOLVED
        and pt.is_flat is True
        and active_exit.is_verified_closed(pt) is True
    )

    return SimulationResult(
        scenario_name="Stop-Loss Scenario",
        passed=passed,
        description="SL triggered at $257.50 (>= $252.50 threshold); BUY orders dispatched and positions confirmed flat.",
        details={
            "entry_premium": float(rm.entry_premium),
            "sl_threshold": float(rm.sl_threshold),
            "breach_buyback": float(event.current_buyback_value),
            "exit_cost": active_exit.total_exit_cost,
            "realized_pnl": pt.get_straddle_position().total_realized_pnl if pt.get_straddle_position() else 0.0,
            "is_flat": pt.is_flat,
        },
    )


# -----------------------------------------------------------------------------
# Scenario 3: Take-Profit Scenario
# -----------------------------------------------------------------------------


def simulate_take_profit_scenario() -> SimulationResult:
    """
    Scenario 3: Take-Profit Scenario
    - Entry: Call 0.05 @ $2500, Put 0.05 @ $2500 -> Entry premium = $250.00.
    - TP Target (-2.0%) = $245.00.
    - Volatility drop: Call ask $2400, Put ask $2400 -> Buyback = $240.00 <= $245.00.
    - Verifies TP triggered, positions liquidated, and positive realized PnL booked.
    """
    call_inst, put_inst, _ = create_sim_straddle_pair(strike=70000.0)
    clock = TestClock()
    om = OrderManager(trader_id=TraderId("TRADER-SIM"), dry_run=True, clock=clock)
    pt = PositionTracker()
    pt.register_straddle_pair(call_inst.id, put_inst.id, 70000.0)
    om.set_position_tracker(pt)

    # Establish entry
    straddle = om.create_straddle_sell_orders(call_inst, put_inst, quantity=0.05, call_price=2500.0, put_price=2500.0)
    om.submit_straddle(straddle)
    om.simulate_acceptance(straddle.call_leg.client_order_id)
    om.simulate_acceptance(straddle.put_leg.client_order_id)
    om.simulate_fill(straddle.call_leg.client_order_id, fill_quantity=0.05, fill_price=2500.0)
    om.simulate_fill(straddle.put_leg.client_order_id, fill_quantity=0.05, fill_price=2500.0)
    pt.apply_fill(call_inst.id, "SELL", 0.05, 2500.0)
    pt.apply_fill(put_inst.id, "SELL", 0.05, 2500.0)

    rm = RiskManager(stop_loss_percent=1.0, take_profit_percent=2.0)
    rm.arm_from_straddle_position(pt.get_straddle_position())
    rm.link_order_manager(om, pt, auto_execute_exit=True)

    # Ingest TP breach tick
    now = datetime.now(timezone.utc)
    rm.update_quote(call_inst.id, bid_price=2380.0, ask_price=2400.0, timestamp_utc=now)
    event = rm.update_quote(put_inst.id, bid_price=2380.0, ask_price=2400.0, timestamp_utc=now)

    assert event is not None and event.trigger_type == RiskTriggerType.TAKE_PROFIT
    active_exit = om.active_exit

    # Complete exit executions
    call_exit_cid = active_exit.call_closing_order_ids[0]
    put_exit_cid = active_exit.put_closing_order_ids[0]
    om.simulate_exit_acceptance(call_exit_cid)
    om.simulate_exit_acceptance(put_exit_cid)
    om.simulate_exit_fill(call_exit_cid, fill_quantity=0.05, fill_price=2400.0)
    om.simulate_exit_fill(put_exit_cid, fill_quantity=0.05, fill_price=2400.0)

    realized = pt.get_straddle_position().total_realized_pnl if pt.get_straddle_position() else 0.0
    passed = (
        active_exit.state == StraddleExitState.EXIT_COMPLETED
        and rm.state == RiskManagerState.RESOLVED
        and pt.is_flat is True
        and realized > 0.0
    )

    return SimulationResult(
        scenario_name="Take-Profit Scenario",
        passed=passed,
        description="TP triggered at $240.00 (<= $245.00 threshold); position closed with positive realized PnL.",
        details={
            "entry_premium": float(rm.entry_premium),
            "tp_threshold": float(rm.tp_threshold),
            "buyback_value": float(event.current_buyback_value),
            "exit_cost": active_exit.total_exit_cost,
            "realized_pnl": realized,
            "is_flat": pt.is_flat,
        },
    )


# -----------------------------------------------------------------------------
# Scenario 4: Call-Only Fill (Unmatched Leg on Entry)
# -----------------------------------------------------------------------------


def simulate_call_only_fill() -> SimulationResult:
    """
    Scenario 4: Call-Only Fill (Entry Unmatched Leg)
    - Call fills 0.05 BTC, Put remains ACCEPTED/unfilled.
    - OrderManager detects ONE_LEG_FILLED.
    - Policy CANCEL_AND_CLOSE executes: Put canceled, Call bought back immediately.
    - Position verified flat, preventing naked short call exposure.
    """
    call_inst, put_inst, _ = create_sim_straddle_pair(strike=70000.0)
    om = OrderManager(trader_id=TraderId("TRADER-SIM"), dry_run=True, unmatched_policy=UnmatchedLegPolicy.CANCEL_AND_CLOSE)
    pt = PositionTracker()
    pt.register_straddle_pair(call_inst.id, put_inst.id, 70000.0)
    om.set_position_tracker(pt)

    straddle = om.create_straddle_sell_orders(call_inst, put_inst, quantity=0.05, call_price=2500.0, put_price=2300.0)
    om.submit_straddle(straddle)
    om.simulate_acceptance(straddle.call_leg.client_order_id)
    om.simulate_acceptance(straddle.put_leg.client_order_id)

    # Call fills, Put remains pending
    om.simulate_fill(straddle.call_leg.client_order_id, fill_quantity=0.05, fill_price=2500.0)
    pt.apply_fill(call_inst.id, "SELL", 0.05, 2500.0)

    assert straddle.status == StraddleExecutionStatus.ONE_LEG_FILLED

    # Resolve unmatched leg
    resolution = om.resolve_unmatched_leg(straddle)
    assert "CANCELED_PUT_LEG" in resolution["actions_taken"]
    assert "SUBMITTED_BUYBACK_FOR_CALL" in resolution["actions_taken"]

    # Fill offsetting buyback order
    closing_cid = straddle.closing_call_order.client_order_id
    om.simulate_acceptance(closing_cid)
    om.simulate_fill(closing_cid, fill_quantity=0.05, fill_price=2510.0)
    pt.apply_fill(call_inst.id, "BUY", 0.05, 2510.0)

    passed = (
        straddle.put_leg.status == LegExecutionStatus.CANCELED
        and pt.is_flat is True
        and straddle.closing_call_order.is_filled is True
    )

    return SimulationResult(
        scenario_name="Call-Only Fill (Unmatched Leg)",
        passed=passed,
        description="Call filled and Put pending detected; pending Put canceled and Call bought back cleanly to flat.",
        details={
            "straddle_status": straddle.status.value,
            "actions_taken": resolution["actions_taken"],
            "is_flat": pt.is_flat,
        },
    )


# -----------------------------------------------------------------------------
# Scenario 5: Put-Only Fill (Unmatched Leg on Entry)
# -----------------------------------------------------------------------------


def simulate_put_only_fill() -> SimulationResult:
    """
    Scenario 5: Put-Only Fill (Entry Unmatched Leg)
    - Put fills 0.05 BTC, Call remains ACCEPTED/unfilled.
    - Policy CANCEL_AND_CLOSE executes: Call canceled, Put bought back immediately.
    - Position verified flat, preventing naked short put exposure.
    """
    call_inst, put_inst, _ = create_sim_straddle_pair(strike=70000.0)
    om = OrderManager(trader_id=TraderId("TRADER-SIM"), dry_run=True, unmatched_policy=UnmatchedLegPolicy.CANCEL_AND_CLOSE)
    pt = PositionTracker()
    pt.register_straddle_pair(call_inst.id, put_inst.id, 70000.0)
    om.set_position_tracker(pt)

    straddle = om.create_straddle_sell_orders(call_inst, put_inst, quantity=0.05, call_price=2500.0, put_price=2300.0)
    om.submit_straddle(straddle)
    om.simulate_acceptance(straddle.call_leg.client_order_id)
    om.simulate_acceptance(straddle.put_leg.client_order_id)

    # Put fills, Call remains pending
    om.simulate_fill(straddle.put_leg.client_order_id, fill_quantity=0.05, fill_price=2300.0)
    pt.apply_fill(put_inst.id, "SELL", 0.05, 2300.0)

    assert straddle.status == StraddleExecutionStatus.ONE_LEG_FILLED

    # Resolve unmatched leg
    resolution = om.resolve_unmatched_leg(straddle)
    assert "CANCELED_CALL_LEG" in resolution["actions_taken"]
    assert "SUBMITTED_BUYBACK_FOR_PUT" in resolution["actions_taken"]

    # Fill offsetting buyback order
    closing_cid = straddle.closing_put_order.client_order_id
    om.simulate_acceptance(closing_cid)
    om.simulate_fill(closing_cid, fill_quantity=0.05, fill_price=2310.0)
    pt.apply_fill(put_inst.id, "BUY", 0.05, 2310.0)

    passed = (
        straddle.call_leg.status == LegExecutionStatus.CANCELED
        and pt.is_flat is True
        and straddle.closing_put_order.is_filled is True
    )

    return SimulationResult(
        scenario_name="Put-Only Fill (Unmatched Leg)",
        passed=passed,
        description="Put filled and Call pending detected; pending Call canceled and Put bought back cleanly to flat.",
        details={
            "straddle_status": straddle.status.value,
            "actions_taken": resolution["actions_taken"],
            "is_flat": pt.is_flat,
        },
    )


# -----------------------------------------------------------------------------
# Scenario 6: Partial Exit (Leaves Reconciliation)
# -----------------------------------------------------------------------------


def simulate_partial_exit_scenario() -> SimulationResult:
    """
    Scenario 6: Partial Exit (Leaves Reconciliation)
    - Exit order for 0.10 BTC is placed.
    - Fills partially (0.04 BTC), then order is canceled/timed out.
    - System calculates leaves: exactly 0.06 BTC remaining.
    - Replacement BUY order for 0.06 BTC submitted and filled.
    - Position verified flat.
    """
    call_inst, put_inst, _ = create_sim_straddle_pair(strike=70000.0)
    om = OrderManager(trader_id=TraderId("TRADER-SIM"), dry_run=True)
    pt = PositionTracker()
    pt.register_straddle_pair(call_inst.id, put_inst.id, 70000.0)
    om.set_position_tracker(pt)

    # Establish 0.10 BTC short straddle
    straddle = om.create_straddle_sell_orders(call_inst, put_inst, quantity=0.10, call_price=2500.0, put_price=2300.0)
    om.submit_straddle(straddle)
    om.simulate_acceptance(straddle.call_leg.client_order_id)
    om.simulate_acceptance(straddle.put_leg.client_order_id)
    om.simulate_fill(straddle.call_leg.client_order_id, fill_quantity=0.10, fill_price=2500.0)
    om.simulate_fill(straddle.put_leg.client_order_id, fill_quantity=0.10, fill_price=2300.0)
    pt.apply_fill(call_inst.id, "SELL", 0.10, 2500.0)
    pt.apply_fill(put_inst.id, "SELL", 0.10, 2300.0)

    # Trigger exit
    exit_exec = om.initiate_straddle_exit(trigger_reason="Test partial exit leaves", straddle=straddle, position_tracker=pt)

    call_cid = exit_exec.call_closing_order_ids[0]
    put_cid = exit_exec.put_closing_order_ids[0]
    om.simulate_exit_acceptance(call_cid)
    om.simulate_exit_acceptance(put_cid)

    # Call fills 100%
    om.simulate_exit_fill(call_cid, fill_quantity=0.10, fill_price=2600.0)
    # Put fills only 0.04 BTC out of 0.10 BTC
    om.simulate_exit_fill(put_cid, fill_quantity=0.04, fill_price=2400.0)

    assert exit_exec.state == StraddleExitState.EXIT_PARTIALLY_FILLED
    assert exit_exec.remaining_put_quantity == 0.06

    # Cancel remaining part of put order
    om.simulate_cancellation(put_cid)

    # Submit remaining required leaves for Put
    retry_tracker = om.submit_remaining_exit_order(OptionLegType.PUT, exit_execution=exit_exec, position_tracker=pt)
    assert retry_tracker is not None
    assert retry_tracker.target_quantity == 0.06

    # Fill retry order
    om.simulate_exit_acceptance(retry_tracker.client_order_id)
    om.simulate_exit_fill(retry_tracker.client_order_id, fill_quantity=0.06, fill_price=2410.0)

    passed = (
        exit_exec.state == StraddleExitState.EXIT_COMPLETED
        and exit_exec.closed_put_quantity == 0.10
        and pt.is_flat is True
    )

    return SimulationResult(
        scenario_name="Partial Exit & Leaves Reconciliation",
        passed=passed,
        description="Exit order partially filled 0.04 BTC; exactly 0.06 BTC leaves resubmitted and filled to reach flat.",
        details={
            "initial_put_close": exit_exec.target_put_close_quantity,
            "closed_put_qty": exit_exec.closed_put_quantity,
            "retry_qty": retry_tracker.target_quantity,
            "is_flat": pt.is_flat,
        },
    )


# -----------------------------------------------------------------------------
# Scenario 7: Exit Rejection Scenario
# -----------------------------------------------------------------------------


def simulate_exit_rejection_scenario() -> SimulationResult:
    """
    Scenario 7: Exit Rejection Scenario
    - Exit order rejected by exchange venue.
    - Execution state transitions to EXIT_FAILED.
    - Captures rejection error reason and maintains audit integrity.
    """
    call_inst, put_inst, _ = create_sim_straddle_pair(strike=70000.0)
    om = OrderManager(trader_id=TraderId("TRADER-SIM"), dry_run=True)
    pt = PositionTracker()
    pt.register_straddle_pair(call_inst.id, put_inst.id, 70000.0)
    om.set_position_tracker(pt)

    straddle = om.create_straddle_sell_orders(call_inst, put_inst, quantity=0.05, call_price=2500.0, put_price=2300.0)
    om.submit_straddle(straddle)
    om.simulate_acceptance(straddle.call_leg.client_order_id)
    om.simulate_acceptance(straddle.put_leg.client_order_id)
    om.simulate_fill(straddle.call_leg.client_order_id, fill_quantity=0.05, fill_price=2500.0)
    om.simulate_fill(straddle.put_leg.client_order_id, fill_quantity=0.05, fill_price=2300.0)
    pt.apply_fill(call_inst.id, "SELL", 0.05, 2500.0)
    pt.apply_fill(put_inst.id, "SELL", 0.05, 2300.0)

    exit_exec = om.initiate_straddle_exit(trigger_reason="Test rejection scenario", straddle=straddle, position_tracker=pt)
    call_cid = exit_exec.call_closing_order_ids[0]

    rejection_msg = "10006: Position liquidation limit exceeded"
    om.simulate_exit_rejection(call_cid, reason=rejection_msg)

    passed = (
        exit_exec.state == StraddleExitState.EXIT_FAILED
        and exit_exec.failure_reason == rejection_msg
        and exit_exec.is_terminal is True
        and exit_exec.is_verified_closed(pt) is False
    )

    return SimulationResult(
        scenario_name="Exit Rejection Handling",
        passed=passed,
        description="Venue rejected closing BUY order; state moved to EXIT_FAILED and recorded error message.",
        details={
            "state": exit_exec.state.value,
            "failure_reason": exit_exec.failure_reason,
            "is_verified_closed": exit_exec.is_verified_closed(pt),
        },
    )


# -----------------------------------------------------------------------------
# Scenario 8: Duplicate Trigger Scenario
# -----------------------------------------------------------------------------


def simulate_duplicate_trigger_scenario() -> SimulationResult:
    """
    Scenario 8: Duplicate Trigger Scenario
    - Rapid successive market data ticks breach Stop-Loss threshold.
    - Verifies idempotency: RiskManager and OrderManager ignore duplicate triggers.
    - Exactly one set of exit orders is submitted.
    """
    call_inst, put_inst, _ = create_sim_straddle_pair(strike=70000.0)
    om = OrderManager(trader_id=TraderId("TRADER-SIM"), dry_run=True)
    pt = PositionTracker()
    pt.register_straddle_pair(call_inst.id, put_inst.id, 70000.0)
    om.set_position_tracker(pt)

    straddle = om.create_straddle_sell_orders(call_inst, put_inst, quantity=0.05, call_price=2500.0, put_price=2500.0)
    om.submit_straddle(straddle)
    om.simulate_acceptance(straddle.call_leg.client_order_id)
    om.simulate_acceptance(straddle.put_leg.client_order_id)
    om.simulate_fill(straddle.call_leg.client_order_id, fill_quantity=0.05, fill_price=2500.0)
    om.simulate_fill(straddle.put_leg.client_order_id, fill_quantity=0.05, fill_price=2500.0)
    pt.apply_fill(call_inst.id, "SELL", 0.05, 2500.0)
    pt.apply_fill(put_inst.id, "SELL", 0.05, 2500.0)

    rm = RiskManager(stop_loss_percent=1.0, take_profit_percent=2.0)
    rm.arm_from_straddle_position(pt.get_straddle_position())
    rm.link_order_manager(om, pt, auto_execute_exit=True)

    now = datetime.now(timezone.utc)
    # First breach tick -> triggers exit
    rm.update_quote(call_inst.id, bid_price=2580.0, ask_price=2600.0, timestamp_utc=now)
    event1 = rm.update_quote(put_inst.id, bid_price=2530.0, ask_price=2550.0, timestamp_utc=now)
    assert event1 is not None

    exit_exec_1 = om.active_exit
    order_count_call_1 = len(exit_exec_1.call_closing_order_ids)
    order_count_put_1 = len(exit_exec_1.put_closing_order_ids)

    # Second breach tick immediately after -> must be ignored
    event2 = rm.update_quote(put_inst.id, bid_price=2540.0, ask_price=2560.0, timestamp_utc=now + timedelta(milliseconds=10))
    assert event2 is None  # Blocked by RiskManager duplicate guard

    # Also directly invoke initiate_straddle_exit
    exit_exec_2 = om.initiate_straddle_exit(trigger_reason="Duplicate call direct", straddle=straddle, position_tracker=pt)

    passed = (
        exit_exec_1 is exit_exec_2
        and len(exit_exec_1.call_closing_order_ids) == order_count_call_1
        and len(exit_exec_1.put_closing_order_ids) == order_count_put_1
    )

    return SimulationResult(
        scenario_name="Duplicate Trigger Prevention",
        passed=passed,
        description="Duplicate trigger ticks and calls were rejected; exactly one coordinated exit was executed.",
        details={
            "call_orders_emitted": len(exit_exec_1.call_closing_order_ids),
            "put_orders_emitted": len(exit_exec_1.put_closing_order_ids),
            "is_idempotent": exit_exec_1 is exit_exec_2,
        },
    )


# -----------------------------------------------------------------------------
# Master Simulation Suite Runner
# -----------------------------------------------------------------------------


def run_all_level2_simulations() -> List[SimulationResult]:
    """Execute all 8 deterministic simulation scenarios sequentially."""
    scenarios = [
        simulate_normal_market_movement,
        simulate_stop_loss_scenario,
        simulate_take_profit_scenario,
        simulate_call_only_fill,
        simulate_put_only_fill,
        simulate_partial_exit_scenario,
        simulate_exit_rejection_scenario,
        simulate_duplicate_trigger_scenario,
    ]

    results: List[SimulationResult] = []
    for scenario_fn in scenarios:
        try:
            res = scenario_fn()
            results.append(res)
            logger.info(res.summary())
        except Exception as e:
            logger.error(f"Scenario failed with exception: {e}", exc_info=True)
            results.append(
                SimulationResult(
                    scenario_name=scenario_fn.__name__,
                    passed=False,
                    description=f"Unhandled exception: {e}",
                    error_message=str(e),
                )
            )

    return results
