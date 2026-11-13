"""
Automated Straddle Risk Manager (Part 11).

Assignment: Mernplus Technologies BTC Options Straddle
Stage: Part 11 - Automated Risk Manager

This module implements continuous, institutional-grade risk monitoring for an active
short straddle position on Bybit and NautilusTrader.

Parameters:
    STOP_LOSS_PERCENT   = 1.0%  (Buyback cost >= 101% of Entry Premium)
    TAKE_PROFIT_PERCENT = 2.0%  (Buyback cost <= 98% of Entry Premium)

Core Trigger Invariants:
    IF CURRENT_BUYBACK_VALUE >= SL_THRESHOLD:
        TRIGGER STOP LOSS (Risk threshold breached; initiate defensive liquidation)
    ELSE IF CURRENT_BUYBACK_VALUE <= TP_THRESHOLD:
        TRIGGER TAKE PROFIT (Theta target reached; initiate profit-taking liquidation)

Requirements Enforced:
1. Dual-Leg Monitoring: Continuously evaluates combined Call and Put buyback liability.
2. Quote Validation: Strictly rejects non-positive, missing, crossed, or zero quotes.
3. Freshness Verification: Quotes older than max_quote_age_seconds (default 5.0s) are ignored.
4. Unit Compatibility: Computations use fixed-precision Decimal types and actual contract multipliers.
5. Actual Entry Fills: Uses real executed quantities and entry VWAPs, never requested order sizes.
6. Market Noise Immunity: Corrupted or transient invalid ticks do NOT trigger false liquidations.
7. Mutual Exclusivity: Prevents simultaneous SL and TP triggers.
8. Idempotency: Prevents duplicate exit initiation once triggered.
9. Comprehensive Audit Trail: Records trigger reason, exact prices, PnL, and timestamps.
10. State Gating: Risk monitoring is strictly disabled until a valid, established two-leg
    short position is armed.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import logging
import time
from typing import Callable, Optional, Tuple, Union

from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.identifiers import InstrumentId

from strategy.position_tracker import StraddlePosition
from utils.calculations import (
    DEFAULT_CONTRACT_MULTIPLIER,
    DEFAULT_SL_FACTOR,
    DEFAULT_TP_FACTOR,
    InvalidCalculationInputError,
    MissingQuoteError,
    calculate_entry_premium,
    calculate_live_buyback_value,
    calculate_pnl_percent,
    calculate_stop_loss_threshold,
    calculate_take_profit_threshold,
    calculate_unrealized_pnl,
    is_stop_loss_triggered,
    is_take_profit_triggered,
    to_decimal,
)

logger = logging.getLogger("btc_straddle.risk_manager")


# -----------------------------------------------------------------------------
# Enums & Custom Exceptions
# -----------------------------------------------------------------------------


class RiskManagerState(str, Enum):
    """Lifecycle states of the Straddle Risk Manager."""

    DISARMED = "DISARMED"            # Position not yet established or closed; monitoring inactive
    ARMED = "ARMED"                  # Valid two-leg short position active; continuous monitoring live
    TRIGGERED = "TRIGGERED"          # Threshold breached; exit signal emitted; duplicate triggers blocked
    RESOLVED = "RESOLVED"            # Position confirmed liquidated; monitoring finalized


class RiskTriggerType(str, Enum):
    """Categorization of straddle risk exit triggers."""

    NONE = "NONE"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"


class RiskManagementError(Exception):
    """Base exception for risk management operations."""


class PositionNotEstablishedError(RiskManagementError):
    """Raised when attempting to arm risk manager without a valid two-leg position."""


# -----------------------------------------------------------------------------
# Trigger Event Record
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskTriggerEvent:
    """
    Immutable audit record generated when a risk threshold (SL or TP) is breached.
    Captures complete market state, entry metrics, and timestamp at trigger moment.
    """

    trigger_type: RiskTriggerType
    trigger_reason: str
    entry_premium: Decimal
    threshold_value: Decimal
    current_buyback_value: Decimal

    call_instrument_id: str
    call_ask: Decimal
    call_quantity: Decimal

    put_instrument_id: str
    put_ask: Decimal
    put_quantity: Decimal

    unrealized_pnl: Decimal
    pnl_percent: Decimal

    trigger_timestamp_utc: datetime
    trigger_timestamp_ns: int
    settlement_currency: str = "USDT"

    def summary(self) -> str:
        """Formatted human-readable trigger summary."""
        sign = "+" if self.unrealized_pnl >= 0 else ""
        return (
            f"[{self.trigger_type.value}] Reason: {self.trigger_reason} | "
            f"Entry: ${self.entry_premium:.2f} {self.settlement_currency} | "
            f"Buyback: ${self.current_buyback_value:.2f} | "
            f"Threshold: ${self.threshold_value:.2f} | "
            f"PnL: {sign}${self.unrealized_pnl:.2f} ({sign}{self.pnl_percent:.2f}%) | "
            f"Call Ask: ${self.call_ask:.2f} | Put Ask: ${self.put_ask:.2f}"
        )


# -----------------------------------------------------------------------------
# Internal Quote Cache
# -----------------------------------------------------------------------------


@dataclass
class CachedQuote:
    """Internal cache entry for an individual option leg's market quote."""

    bid_price: Decimal
    ask_price: Decimal
    timestamp_utc: datetime
    timestamp_ns: int

    def is_valid(self) -> bool:
        """Quote is valid if prices are positive and spread is non-negative."""
        return (
            self.bid_price > Decimal("0")
            and self.ask_price > Decimal("0")
            and self.ask_price >= self.bid_price
        )

    def age_seconds(self, as_of: Optional[datetime] = None) -> float:
        """Elapsed time since quote creation in seconds."""
        now = as_of or datetime.now(timezone.utc)
        return max(0.0, (now - self.timestamp_utc).total_seconds())


# -----------------------------------------------------------------------------
# Automated Straddle Risk Manager Engine
# -----------------------------------------------------------------------------


class RiskManager:
    """
    Continuous Risk Management Engine for BTC Short Straddle Strategies.

    Monitors real-time ask-side buyback valuations against fixed entry credit,
    evaluating Stop-Loss and Take-Profit boundaries with nanosecond precision.
    """

    def __init__(
        self,
        stop_loss_percent: float = 1.0,
        take_profit_percent: float = 2.0,
        max_quote_age_seconds: float = 5.0,
        contract_multiplier: Union[Decimal, float] = DEFAULT_CONTRACT_MULTIPLIER,
        on_trigger_callback: Optional[Callable[[RiskTriggerEvent], None]] = None,
    ):
        if stop_loss_percent <= 0.0:
            raise ValueError(f"stop_loss_percent must be positive: {stop_loss_percent}")
        if take_profit_percent <= 0.0:
            raise ValueError(f"take_profit_percent must be positive: {take_profit_percent}")
        if max_quote_age_seconds <= 0.0:
            raise ValueError(f"max_quote_age_seconds must be positive: {max_quote_age_seconds}")

        self.stop_loss_percent = to_decimal(stop_loss_percent, "stop_loss_percent")
        self.take_profit_percent = to_decimal(take_profit_percent, "take_profit_percent")
        self.max_quote_age_seconds = float(max_quote_age_seconds)
        self.contract_multiplier = to_decimal(contract_multiplier, "contract_multiplier")
        self.on_trigger_callback = on_trigger_callback

        # Multipliers for thresholds
        # SL: +1.0% loss -> Buyback reaches 101% of entry premium
        self.sl_factor = Decimal("1.0") + (self.stop_loss_percent / Decimal("100.0"))
        # TP: +2.0% profit -> Buyback drops to 98% of entry premium
        self.tp_factor = Decimal("1.0") - (self.take_profit_percent / Decimal("100.0"))

        # State management
        self.state: RiskManagerState = RiskManagerState.DISARMED

        # Position tracking data (populated on arm)
        self.call_instrument_id: Optional[InstrumentId] = None
        self.put_instrument_id: Optional[InstrumentId] = None
        self.call_executed_qty: Decimal = Decimal("0")
        self.put_executed_qty: Decimal = Decimal("0")
        self.call_entry_vwap: Decimal = Decimal("0")
        self.put_entry_vwap: Decimal = Decimal("0")
        self.entry_premium: Decimal = Decimal("0")
        self.sl_threshold: Decimal = Decimal("0")
        self.tp_threshold: Decimal = Decimal("0")

        # Live quote caches
        self._cached_quotes: dict[str, CachedQuote] = {}

        # Valuation & trigger results
        self.latest_buyback_value: Optional[Decimal] = None
        self.latest_unrealized_pnl: Optional[Decimal] = None
        self.latest_pnl_percent: Optional[Decimal] = None
        self.trigger_event: Optional[RiskTriggerEvent] = None

        logger.info(
            f"RiskManager initialized: SL={self.stop_loss_percent}% (Factor={self.sl_factor}), "
            f"TP={self.take_profit_percent}% (Factor={self.tp_factor}), "
            f"MaxQuoteAge={self.max_quote_age_seconds}s, Multiplier={self.contract_multiplier}"
        )

    # -------------------------------------------------------------------------
    # 1. State Inspection Properties
    # -------------------------------------------------------------------------

    @property
    def is_armed(self) -> bool:
        """True if the risk manager is actively monitoring an established position."""
        return self.state == RiskManagerState.ARMED

    @property
    def is_triggered(self) -> bool:
        """True if an exit condition (SL or TP) has fired."""
        return self.state in (RiskManagerState.TRIGGERED, RiskManagerState.RESOLVED)

    @property
    def trigger_type(self) -> RiskTriggerType:
        """Current trigger classification."""
        if self.trigger_event:
            return self.trigger_event.trigger_type
        return RiskTriggerType.NONE

    # -------------------------------------------------------------------------
    # 2. Arming & Disarming (Pre-Condition Enforcement)
    # -------------------------------------------------------------------------

    def arm(
        self,
        call_instrument_id: Union[InstrumentId, str],
        put_instrument_id: Union[InstrumentId, str],
        call_executed_qty: Union[Decimal, float, int, str],
        call_entry_vwap: Union[Decimal, float, int, str],
        put_executed_qty: Union[Decimal, float, int, str],
        put_entry_vwap: Union[Decimal, float, int, str],
        call_multiplier: Union[Decimal, float] = DEFAULT_CONTRACT_MULTIPLIER,
        put_multiplier: Union[Decimal, float] = DEFAULT_CONTRACT_MULTIPLIER,
    ) -> None:
        """
        Arm the risk manager with a fully established, executed two-leg short straddle position.

        PRE-CONDITION:
        Must have non-zero, positive executed quantities and prices for BOTH Call and Put legs.
        Raises PositionNotEstablishedError if either leg is missing or zero.
        """
        c_id = (
            call_instrument_id
            if isinstance(call_instrument_id, InstrumentId)
            else InstrumentId.from_str(str(call_instrument_id))
        )
        p_id = (
            put_instrument_id
            if isinstance(put_instrument_id, InstrumentId)
            else InstrumentId.from_str(str(put_instrument_id))
        )

        c_qty = to_decimal(call_executed_qty, "call_executed_qty")
        p_qty = to_decimal(put_executed_qty, "put_executed_qty")
        c_px = to_decimal(call_entry_vwap, "call_entry_vwap")
        p_px = to_decimal(put_entry_vwap, "put_entry_vwap")
        c_mult = to_decimal(call_multiplier, "call_multiplier")
        p_mult = to_decimal(put_multiplier, "put_multiplier")

        # Validate that BOTH legs have non-zero, positive fills
        if c_qty <= Decimal("0") or p_qty <= Decimal("0"):
            raise PositionNotEstablishedError(
                f"Cannot arm RiskManager without a valid two-leg position: "
                f"Call Qty={c_qty}, Put Qty={p_qty}. Both legs must be strictly positive."
            )
        if c_px <= Decimal("0") or p_px <= Decimal("0"):
            raise PositionNotEstablishedError(
                f"Cannot arm RiskManager with invalid entry prices: "
                f"Call VWAP={c_px}, Put VWAP={p_px}."
            )

        self.call_instrument_id = c_id
        self.put_instrument_id = p_id
        self.call_executed_qty = c_qty
        self.put_executed_qty = p_qty
        self.call_entry_vwap = c_px
        self.put_entry_vwap = p_px

        # Calculate exact gross entry premium
        self.entry_premium = calculate_entry_premium(
            call_entry_price=c_px,
            call_quantity=c_qty,
            put_entry_price=p_px,
            put_quantity=p_qty,
            call_multiplier=c_mult,
            put_multiplier=p_mult,
        )

        # Compute dynamic thresholds
        self.sl_threshold = calculate_stop_loss_threshold(self.entry_premium, self.sl_factor)
        self.tp_threshold = calculate_take_profit_threshold(self.entry_premium, self.tp_factor)

        self.state = RiskManagerState.ARMED
        self.trigger_event = None

        logger.info(
            f"RiskManager ARMED for straddle: Call={c_id} ({c_qty} @ ${c_px:.2f}), "
            f"Put={p_id} ({p_qty} @ ${p_px:.2f})\n"
            f"  Gross Entry Premium: ${self.entry_premium:.2f} USDT\n"
            f"  Stop-Loss Threshold:  ${self.sl_threshold:.2f} USDT (+{self.stop_loss_percent}% liability rise)\n"
            f"  Take-Profit Target:   ${self.tp_threshold:.2f} USDT (-{self.take_profit_percent}% liability fall)"
        )

    def arm_from_straddle_position(self, straddle_pos: StraddlePosition) -> None:
        """Convenience method to arm RiskManager directly from an active StraddlePosition model."""
        if not straddle_pos:
            raise PositionNotEstablishedError("StraddlePosition cannot be None.")

        call_leg = straddle_pos.call_position
        put_leg = straddle_pos.put_position

        if not call_leg.is_open or not put_leg.is_open:
            raise PositionNotEstablishedError(
                f"Both legs must be open in StraddlePosition: Call open={call_leg.is_open}, Put open={put_leg.is_open}"
            )

        self.arm(
            call_instrument_id=call_leg.instrument_id,
            put_instrument_id=put_leg.instrument_id,
            call_executed_qty=call_leg.quantity,
            call_entry_vwap=call_leg.entry_vwap,
            put_executed_qty=put_leg.quantity,
            put_entry_vwap=put_leg.entry_vwap,
            call_multiplier=self.contract_multiplier,
            put_multiplier=self.contract_multiplier,
        )

    def disarm(self) -> None:
        """Disarm the risk manager and cease all evaluations."""
        self.state = RiskManagerState.DISARMED
        logger.info("RiskManager DISARMED.")

    def reset(self) -> None:
        """Reset internal caches and trigger state."""
        self.disarm()
        self._cached_quotes.clear()
        self.trigger_event = None
        self.latest_buyback_value = None
        self.latest_unrealized_pnl = None
        self.latest_pnl_percent = None

    # -------------------------------------------------------------------------
    # 3. Market Quote Ingestion & Evaluation
    # -------------------------------------------------------------------------

    def on_quote_tick(self, tick: QuoteTick) -> Optional[RiskTriggerEvent]:
        """
        Process an incoming NautilusTrader QuoteTick event.
        Updates internal quote cache for the instrument and evaluates risk conditions.

        Returns:
            RiskTriggerEvent if Stop-Loss or Take-Profit is breached for the first time.
            None otherwise.
        """
        inst_id_str = tick.instrument_id.value
        bid = float(tick.bid_price)
        ask = float(tick.ask_price)
        ts_ns = int(tick.ts_event)
        ts_utc = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)

        return self.update_quote(inst_id_str, bid, ask, ts_utc, ts_ns)

    def update_quote(
        self,
        instrument_id: Union[InstrumentId, str],
        bid_price: Union[Decimal, float, int, str],
        ask_price: Union[Decimal, float, int, str],
        timestamp_utc: Optional[datetime] = None,
        timestamp_ns: Optional[int] = None,
        as_of: Optional[datetime] = None,
    ) -> Optional[RiskTriggerEvent]:
        """
        Update market quote for a specific instrument and evaluate combined straddle risk.
        """
        inst_key = str(instrument_id)
        quote_ts_utc = timestamp_utc or datetime.now(timezone.utc)
        ts_ns = timestamp_ns or int(time.time() * 1e9)

        try:
            bid_dec = to_decimal(bid_price, "bid_price")
            ask_dec = to_decimal(ask_price, "ask_price")
        except InvalidCalculationInputError as e:
            logger.debug(f"Invalid quote values for {inst_key}: {e}. Ignoring tick.")
            return None

        # Cache quote
        self._cached_quotes[inst_key] = CachedQuote(
            bid_price=bid_dec,
            ask_price=ask_dec,
            timestamp_utc=quote_ts_utc,
            timestamp_ns=ts_ns,
        )

        # Evaluate risk conditions against evaluation time (or current wall-clock)
        eval_time = as_of or datetime.now(timezone.utc)
        return self.evaluate_current_risk(as_of=eval_time)

    # -------------------------------------------------------------------------
    # 4. Continuous Risk Evaluation Core
    # -------------------------------------------------------------------------

    def evaluate_current_risk(
        self,
        as_of: Optional[datetime] = None,
    ) -> Optional[RiskTriggerEvent]:
        """
        Evaluate current combined straddle buyback value against SL and TP thresholds.

        ENFORCEMENT:
        1. Refuses to evaluate if state is NOT ARMED.
        2. Refuses to evaluate if already TRIGGERED (prevents duplicate exits).
        3. Validates both quotes: non-negative, spread positive, ask > 0.
        4. Validates freshness: age <= max_quote_age_seconds.
        5. Computes buyback value: (Call Ask * Call Qty) + (Put Ask * Put Qty).
        6. Checks:
             IF buyback >= SL_THRESHOLD -> TRIGGER STOP LOSS
             ELSE IF buyback <= TP_THRESHOLD -> TRIGGER TAKE PROFIT
        """
        # 1. State Guard: Only evaluate if ARMED
        if self.state != RiskManagerState.ARMED:
            return None

        # 2. Duplicate Guard: Once triggered, do not re-trigger
        if self.trigger_event is not None:
            return None

        if not self.call_instrument_id or not self.put_instrument_id:
            return None

        call_key = self.call_instrument_id.value
        put_key = self.put_instrument_id.value

        call_quote = self._cached_quotes.get(call_key)
        put_quote = self._cached_quotes.get(put_key)

        # 3. Missing Quotes Check: Both legs must be present in cache
        if call_quote is None or put_quote is None:
            logger.debug(
                f"Risk evaluation waiting for quotes: Call present={call_quote is not None}, "
                f"Put present={put_quote is not None}"
            )
            return None

        # 4. Quote Validity Check (spreads, zero/negative prices)
        if not call_quote.is_valid() or not put_quote.is_valid():
            logger.debug("Risk evaluation ignored: Invalid or crossed quote detected.")
            return None

        # 5. Quote Freshness Check
        now = as_of or datetime.now(timezone.utc)
        call_age = call_quote.age_seconds(now)
        put_age = put_quote.age_seconds(now)

        if call_age > self.max_quote_age_seconds or put_age > self.max_quote_age_seconds:
            logger.debug(
                f"Risk evaluation ignored: Stale quote detected (Call age={call_age:.2f}s, "
                f"Put age={put_age:.2f}s > {self.max_quote_age_seconds}s limit)."
            )
            return None

        # 6. Compute Live Combined Buyback Liability
        try:
            buyback_value = calculate_live_buyback_value(
                call_ask=call_quote.ask_price,
                call_quantity=self.call_executed_qty,
                put_ask=put_quote.ask_price,
                put_quantity=self.put_executed_qty,
                call_multiplier=self.contract_multiplier,
                put_multiplier=self.contract_multiplier,
            )
        except MissingQuoteError as e:
            logger.debug(f"Risk evaluation quote error: {e}")
            return None

        # Compute unrealized PnL metrics
        unrealized_pnl = calculate_unrealized_pnl(self.entry_premium, buyback_value)
        pnl_pct = calculate_pnl_percent(self.entry_premium, buyback_value)

        self.latest_buyback_value = buyback_value
        self.latest_unrealized_pnl = unrealized_pnl
        self.latest_pnl_percent = pnl_pct

        # 7. Check Triggers (Mutually Exclusive)
        trigger_type = RiskTriggerType.NONE
        trigger_reason = ""
        threshold_val = Decimal("0")

        # Condition A: STOP-LOSS
        # Buyback cost rises to or above SL threshold (e.g. >= 101% of entry premium)
        if is_stop_loss_triggered(buyback_value, self.sl_threshold):
            trigger_type = RiskTriggerType.STOP_LOSS
            threshold_val = self.sl_threshold
            trigger_reason = (
                f"Buyback value ${buyback_value:.2f} reached or exceeded Stop-Loss threshold "
                f"${self.sl_threshold:.2f} (Entry: ${self.entry_premium:.2f}, Loss: {pnl_pct:.2f}%)"
            )

        # Condition B: TAKE-PROFIT (Mutually exclusive via elif)
        # Buyback cost declines to or below TP threshold (e.g. <= 98% of entry premium)
        elif is_take_profit_triggered(buyback_value, self.tp_threshold):
            trigger_type = RiskTriggerType.TAKE_PROFIT
            threshold_val = self.tp_threshold
            trigger_reason = (
                f"Buyback value ${buyback_value:.2f} reached or fell below Take-Profit threshold "
                f"${self.tp_threshold:.2f} (Entry: ${self.entry_premium:.2f}, Gain: +{pnl_pct:.2f}%)"
            )

        # If triggered, build event, transition state, and notify callback
        if trigger_type != RiskTriggerType.NONE:
            event = RiskTriggerEvent(
                trigger_type=trigger_type,
                trigger_reason=trigger_reason,
                entry_premium=self.entry_premium,
                threshold_value=threshold_val,
                current_buyback_value=buyback_value,
                call_instrument_id=call_key,
                call_ask=call_quote.ask_price,
                call_quantity=self.call_executed_qty,
                put_instrument_id=put_key,
                put_ask=put_quote.ask_price,
                put_quantity=self.put_executed_qty,
                unrealized_pnl=unrealized_pnl,
                pnl_percent=pnl_pct,
                trigger_timestamp_utc=now,
                trigger_timestamp_ns=int(time.time() * 1e9),
            )

            # Transition state to TRIGGERED to prevent duplicate exits
            self.state = RiskManagerState.TRIGGERED
            self.trigger_event = event

            logger.warning(f"CRITICAL RISK EVENT: {event.summary()}")

            # Fire callback if registered
            if self.on_trigger_callback is not None:
                try:
                    self.on_trigger_callback(event)
                except Exception as cb_err:
                    logger.error(f"Error in on_trigger_callback: {cb_err}", exc_info=True)

            return event

        return None
