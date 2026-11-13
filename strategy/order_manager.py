"""
Automated BTC Options Straddle - Order Management Engine.

Provides dual-leg order creation, unique client order ID generation, quantity validation,
independent leg lifecycle tracking, state machine management, execution reconciliation,
and configurable unmatched-leg resolution (Call filled, Put pending scenario).

All orders and events interface directly with official NautilusTrader model classes:
- nautilus_trader.model.orders.LimitOrder / MarketOrder
- nautilus_trader.model.events.OrderSubmitted / OrderAccepted / OrderFilled / OrderCanceled / OrderRejected
- nautilus_trader.common.factories.OrderFactory
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from nautilus_trader.common.component import Clock, LiveClock, TestClock
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.enums import (
    LiquiditySide,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderEvent,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    StrategyId,
    TradeId,
    TraderId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import CryptoOption, Instrument
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.model.orders import LimitOrder, MarketOrder, Order

logger = logging.getLogger("btc_straddle.order_manager")


# -----------------------------------------------------------------------------
# Custom Exceptions
# -----------------------------------------------------------------------------


class OrderManagementError(Exception):
    """Base exception for order management operations."""


class DuplicateOrderError(OrderManagementError):
    """Raised when an order or client order ID is duplicated."""


class InvalidQuantityError(OrderManagementError):
    """Raised when an order quantity violates lot size, step, or precision rules."""


class UnmatchedLegError(OrderManagementError):
    """Raised when an orphan or unmatched leg cannot be resolved."""


class OrderSubmissionError(OrderManagementError):
    """Raised when order submission fails."""


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------


class OptionLegType(str, Enum):
    """Type of option leg in a straddle structure."""

    CALL = "CALL"
    PUT = "PUT"


class LegExecutionStatus(str, Enum):
    """
    Independent leg execution lifecycle states.
    Strictly adheres to the required state machine:
    NOT_SUBMITTED -> SUBMITTED -> ACCEPTED -> PARTIALLY_FILLED -> FILLED / CANCELED / REJECTED
    """

    NOT_SUBMITTED = "NOT_SUBMITTED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class StraddleExecutionStatus(str, Enum):
    """
    Composite status of the dual-leg straddle structure.
    Recognizes the critical 'ONE_LEG_FILLED' scenario.
    """

    NOT_SUBMITTED = "NOT_SUBMITTED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    ONE_LEG_FILLED = "ONE_LEG_FILLED"  # One leg filled while the other is pending/accepted
    COMPLETED = "COMPLETED"            # Both Call and Put legs fully filled
    CANCELED = "CANCELED"              # Both legs canceled or entry aborted
    REJECTED = "REJECTED"              # One or both legs rejected
    RESOLVING_UNMATCHED = "RESOLVING_UNMATCHED"  # Executing unmatched-leg policy (e.g. closing filled leg)


class UnmatchedLegPolicy(str, Enum):
    """
    Configurable policy when one leg fills and the opposing leg remains pending or fails.
    """

    WAIT = "WAIT"                          # Await second leg fill until entry timeout
    CANCEL_REMAINING = "CANCEL_REMAINING"  # Cancel pending leg; keep filled leg as-is
    CANCEL_AND_CLOSE = "CANCEL_AND_CLOSE"  # Cancel pending leg, immediately buy back and close filled leg
    CLOSE_FILLED = "CLOSE_FILLED"          # Alias/shorthand for closing filled leg


# -----------------------------------------------------------------------------
# Leg Fill Record
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class LegFill:
    """Individual execution fill record."""

    trade_id: str
    fill_quantity: float
    fill_price: float
    commission: float
    commission_currency: str
    liquidity_side: str
    timestamp_ns: int


# -----------------------------------------------------------------------------
# Leg Order Tracker
# -----------------------------------------------------------------------------


@dataclass
class LegOrderTracker:
    """
    Tracks an individual option leg (Call or Put) throughout its complete lifecycle.
    Encapsulates the NautilusTrader Order instance and recalculates cumulative fills
    and volume-weighted average price (VWAP) deterministically upon every event.
    """

    leg_type: OptionLegType
    instrument_id: InstrumentId
    order_side: OrderSide  # SELL for short straddle entry, BUY for closing
    target_quantity: float
    target_price: Optional[float] = None
    client_order_id: Optional[ClientOrderId] = None
    venue_order_id: Optional[VenueOrderId] = None
    status: LegExecutionStatus = LegExecutionStatus.NOT_SUBMITTED
    nautilus_order: Optional[Order] = None
    filled_quantity: float = 0.0
    average_price: float = 0.0
    commission_paid: float = 0.0
    rejection_reason: Optional[str] = None
    fills: List[LegFill] = field(default_factory=list)
    events: List[OrderEvent] = field(default_factory=list)
    created_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_terminal(self) -> bool:
        """Return True if the leg has reached a final immutable state."""
        return self.status in (
            LegExecutionStatus.FILLED,
            LegExecutionStatus.CANCELED,
            LegExecutionStatus.REJECTED,
        )

    @property
    def is_filled(self) -> bool:
        """Return True if the leg is completely filled."""
        return self.status == LegExecutionStatus.FILLED

    @property
    def is_pending(self) -> bool:
        """Return True if the order is awaiting submission, acceptance, or fill."""
        return self.status in (
            LegExecutionStatus.SUBMITTED,
            LegExecutionStatus.ACCEPTED,
            LegExecutionStatus.PARTIALLY_FILLED,
        )

    @property
    def is_active(self) -> bool:
        """Return True if the order has been submitted and is not terminal."""
        return not self.is_terminal and self.status != LegExecutionStatus.NOT_SUBMITTED

    @property
    def leaves_quantity(self) -> float:
        """Remaining unfilled quantity."""
        return max(0.0, round(self.target_quantity - self.filled_quantity, 8))

    def apply_event(self, event: OrderEvent) -> None:
        """
        Apply a NautilusTrader OrderEvent to the leg tracker, updating state,
        fills, and volume-weighted average execution price.
        Enforces idempotency so duplicate events (e.g. from network or retry loops)
        do not corrupt state.
        """
        # Guard against duplicate event processing (idempotency)
        event_id_val = getattr(event, "id", None)
        if event_id_val is not None and any(getattr(e, "id", None) == event_id_val for e in self.events):
            logger.debug(
                f"Event {event_id_val} already applied to {self.client_order_id}. Skipping duplicate."
            )
            return

        self.events.append(event)
        self.updated_at_utc = datetime.now(timezone.utc)

        # Sync underlying NautilusTrader Order object state if present
        if self.nautilus_order is not None:
            try:
                self.nautilus_order.apply(event)
            except Exception as e:
                logger.debug(f"Nautilus order.apply noted: {e}")

        if isinstance(event, OrderSubmitted):
            self.status = LegExecutionStatus.SUBMITTED
            logger.info(
                f"[{self.leg_type.value}] Leg order submitted: "
                f"client_order_id={self.client_order_id}, instrument={self.instrument_id}"
            )

        elif isinstance(event, OrderAccepted):
            self.status = LegExecutionStatus.ACCEPTED
            self.venue_order_id = event.venue_order_id
            logger.info(
                f"[{self.leg_type.value}] Leg order accepted: "
                f"client_order_id={self.client_order_id}, venue_order_id={self.venue_order_id}"
            )

        elif isinstance(event, OrderRejected):
            self.status = LegExecutionStatus.REJECTED
            self.rejection_reason = str(event.reason)
            logger.warning(
                f"[{self.leg_type.value}] Leg order rejected: "
                f"client_order_id={self.client_order_id}, reason='{self.rejection_reason}'"
            )

        elif isinstance(event, OrderCanceled):
            self.status = LegExecutionStatus.CANCELED
            logger.info(
                f"[{self.leg_type.value}] Leg order canceled: "
                f"client_order_id={self.client_order_id}"
            )

        elif isinstance(event, OrderFilled):
            fill_qty = float(event.last_qty)
            fill_px = float(event.last_px)
            comm_val = float(event.commission.as_double()) if event.commission else 0.0
            comm_curr = str(event.currency) if event.currency else "USDT"

            # Record individual fill execution
            fill_record = LegFill(
                trade_id=str(event.trade_id),
                fill_quantity=fill_qty,
                fill_price=fill_px,
                commission=comm_val,
                commission_currency=comm_curr,
                liquidity_side=str(event.liquidity_side),
                timestamp_ns=event.ts_event,
            )
            self.fills.append(fill_record)
            self.commission_paid += comm_val

            # Compute new cumulative filled quantity and VWAP average execution price
            prev_filled = self.filled_quantity
            new_filled = prev_filled + fill_qty
            if new_filled > 0:
                self.average_price = (
                    (self.average_price * prev_filled) + (fill_px * fill_qty)
                ) / new_filled
            self.filled_quantity = round(new_filled, 8)

            # Determine whether partially or fully filled
            if self.filled_quantity >= (self.target_quantity - 1e-7):
                self.status = LegExecutionStatus.FILLED
                logger.info(
                    f"[{self.leg_type.value}] Leg order FILLED: "
                    f"filled={self.filled_quantity}/{self.target_quantity} @ avg_px={self.average_price:.2f}"
                )
            else:
                self.status = LegExecutionStatus.PARTIALLY_FILLED
                logger.info(
                    f"[{self.leg_type.value}] Leg order PARTIALLY FILLED: "
                    f"filled={self.filled_quantity}/{self.target_quantity} (leaves={self.leaves_quantity})"
                )


# -----------------------------------------------------------------------------
# Straddle Order (Dual-Leg Structure)
# -----------------------------------------------------------------------------


@dataclass
class StraddleOrder:
    """
    Coordinates the paired Call and Put legs of a straddle structure.
    Monitors synchronization between legs and detects the unmatched leg scenario.
    """

    straddle_id: str
    call_leg: LegOrderTracker
    put_leg: LegOrderTracker
    unmatched_policy: UnmatchedLegPolicy = UnmatchedLegPolicy.CANCEL_AND_CLOSE
    status: StraddleExecutionStatus = StraddleExecutionStatus.NOT_SUBMITTED
    created_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Tracks offsetting closing order if unmatched leg resolution is executed
    closing_call_order: Optional[LegOrderTracker] = None
    closing_put_order: Optional[LegOrderTracker] = None

    @property
    def is_one_leg_filled(self) -> bool:
        """
        Critical check: Returns True if exactly one leg is FILLED while the other
        is NOT filled (i.e. still SUBMITTED, ACCEPTED, PARTIALLY_FILLED, CANCELED, or REJECTED).
        """
        call_f = self.call_leg.is_filled
        put_f = self.put_leg.is_filled
        return (call_f and not put_f) or (put_f and not call_f)

    @property
    def unmatched_filled_leg(self) -> Optional[LegOrderTracker]:
        """Return the filled leg if exactly one leg has filled, else None."""
        if self.call_leg.is_filled and not self.put_leg.is_filled:
            return self.call_leg
        if self.put_leg.is_filled and not self.call_leg.is_filled:
            return self.put_leg
        return None

    @property
    def unmatched_pending_leg(self) -> Optional[LegOrderTracker]:
        """Return the non-filled/pending leg if exactly one leg has filled, else None."""
        if self.call_leg.is_filled and not self.put_leg.is_filled:
            return self.put_leg
        if self.put_leg.is_filled and not self.call_leg.is_filled:
            return self.call_leg
        return None

    @property
    def is_fully_filled(self) -> bool:
        """Return True if both Call and Put legs are completely filled."""
        return self.call_leg.is_filled and self.put_leg.is_filled

    @property
    def is_terminal(self) -> bool:
        """Return True if the entire straddle lifecycle has concluded."""
        return self.status in (
            StraddleExecutionStatus.COMPLETED,
            StraddleExecutionStatus.CANCELED,
            StraddleExecutionStatus.REJECTED,
        )

    @property
    def total_filled_quantity(self) -> float:
        """Aggregate contracts filled across both legs."""
        return round(self.call_leg.filled_quantity + self.put_leg.filled_quantity, 8)

    @property
    def net_premium_collected(self) -> float:
        """
        Gross premium collected from filled contracts (for short straddle entry):
        (call_filled * call_avg_px) + (put_filled * put_avg_px)
        """
        return (
            (self.call_leg.filled_quantity * self.call_leg.average_price)
            + (self.put_leg.filled_quantity * self.put_leg.average_price)
        )

    def evaluate_composite_status(self) -> StraddleExecutionStatus:
        """
        Deterministically recompute composite straddle status based on current leg states.
        """
        c_stat = self.call_leg.status
        p_stat = self.put_leg.status

        # If both are filled -> COMPLETED
        if c_stat == LegExecutionStatus.FILLED and p_stat == LegExecutionStatus.FILLED:
            self.status = StraddleExecutionStatus.COMPLETED

        # If one leg is filled and the other is NOT filled -> ONE_LEG_FILLED
        elif (c_stat == LegExecutionStatus.FILLED and p_stat != LegExecutionStatus.FILLED) or (
            p_stat == LegExecutionStatus.FILLED and c_stat != LegExecutionStatus.FILLED
        ):
            self.status = StraddleExecutionStatus.ONE_LEG_FILLED

        # If one or both are rejected
        elif c_stat == LegExecutionStatus.REJECTED or p_stat == LegExecutionStatus.REJECTED:
            self.status = StraddleExecutionStatus.REJECTED

        # If both are canceled
        elif c_stat == LegExecutionStatus.CANCELED and p_stat == LegExecutionStatus.CANCELED:
            self.status = StraddleExecutionStatus.CANCELED

        # If any leg has partial fills
        elif (
            c_stat == LegExecutionStatus.PARTIALLY_FILLED
            or p_stat == LegExecutionStatus.PARTIALLY_FILLED
        ):
            self.status = StraddleExecutionStatus.PARTIALLY_FILLED

        # If accepted
        elif c_stat == LegExecutionStatus.ACCEPTED and p_stat == LegExecutionStatus.ACCEPTED:
            self.status = StraddleExecutionStatus.ACCEPTED
        elif c_stat == LegExecutionStatus.ACCEPTED or p_stat == LegExecutionStatus.ACCEPTED:
            self.status = StraddleExecutionStatus.ACCEPTED

        # If submitted
        elif c_stat == LegExecutionStatus.SUBMITTED or p_stat == LegExecutionStatus.SUBMITTED:
            self.status = StraddleExecutionStatus.SUBMITTED

        else:
            self.status = StraddleExecutionStatus.NOT_SUBMITTED

        self.updated_at_utc = datetime.now(timezone.utc)
        return self.status


# -----------------------------------------------------------------------------
# Order Manager Implementation
# -----------------------------------------------------------------------------


class OrderManager:
    """
    Production-grade Order Management Engine for BTC Options Straddle.

    Implements:
    - Unique ClientOrderId generation adhering to Bybit length and charset specs.
    - SELL order creation for short straddle entry.
    - Quantity validation against contract multiplier, min lot, and lot step.
    - Execution client order submission with strict DRY_RUN safety.
    - Independent leg lifecycle event tracking (Submitted, Accepted, Rejected, Filled, Canceled).
    - Accurate fill tracking and volume-weighted average price calculation.
    - Duplicate order and duplicate client order ID prevention.
    - Critical 'One Leg Filled' detection and configurable unmatched-leg resolution.
    """

    def __init__(
        self,
        trader_id: Optional[TraderId] = None,
        strategy_id: Optional[StrategyId] = None,
        account_id: Optional[AccountId] = None,
        execution_client: Optional[Any] = None,
        submit_fn: Optional[Callable[[Order], None]] = None,
        cancel_fn: Optional[Callable[[ClientOrderId], None]] = None,
        unmatched_policy: Union[UnmatchedLegPolicy, str] = UnmatchedLegPolicy.CANCEL_AND_CLOSE,
        dry_run: bool = True,
        clock: Optional[Clock] = None,
    ):
        self.trader_id = trader_id or TraderId("STRADDLE-TRADER")
        self.strategy_id = strategy_id or StrategyId("BTC-STRADDLE-001")
        self.account_id = account_id or AccountId("BYBIT-TESTNET-001")
        self.execution_client = execution_client
        self.submit_fn = submit_fn
        self.cancel_fn = cancel_fn
        self.dry_run = dry_run

        # Resolve concrete clock (guard against uninitialized Cython Clock stubs)
        if clock is not None:
            try:
                clock.timestamp_ns()
                self.clock = clock
            except (NotImplementedError, AttributeError):
                self.clock = LiveClock()
        else:
            self.clock = LiveClock()

        # Normalize unmatched leg policy
        if isinstance(unmatched_policy, str):
            policy_str = unmatched_policy.strip().upper()
            if policy_str in ("CLOSE_FILLED", "CANCEL_AND_CLOSE"):
                self.unmatched_policy = UnmatchedLegPolicy.CANCEL_AND_CLOSE
            elif policy_str == "CANCEL_REMAINING":
                self.unmatched_policy = UnmatchedLegPolicy.CANCEL_REMAINING
            elif policy_str == "WAIT":
                self.unmatched_policy = UnmatchedLegPolicy.WAIT
            else:
                self.unmatched_policy = UnmatchedLegPolicy(policy_str)
        else:
            self.unmatched_policy = unmatched_policy

        # Internal OrderFactory for creating NautilusTrader Order instances
        self.order_factory = OrderFactory(
            trader_id=self.trader_id,
            strategy_id=self.strategy_id,
            clock=self.clock,
        )

        # State registries
        self._seq_counter: int = 0
        self._seen_client_order_ids: Set[str] = set()
        self._orders_by_client_id: Dict[ClientOrderId, LegOrderTracker] = {}
        self._straddles: List[StraddleOrder] = []
        self._active_straddle: Optional[StraddleOrder] = None

        logger.info(
            f"OrderManager initialized: dry_run={self.dry_run}, "
            f"unmatched_policy={self.unmatched_policy.value}, "
            f"trader_id={self.trader_id}, strategy_id={self.strategy_id}"
        )

    # -------------------------------------------------------------------------
    # 1. Unique Client Order ID Generation
    # -------------------------------------------------------------------------

    def generate_client_order_id(
        self,
        leg_type: OptionLegType,
        tag: str = "",
    ) -> ClientOrderId:
        """
        Generate a unique, monotonic, collision-free ClientOrderId.
        Adheres to Bybit V5 requirements: max 36 characters, alphanumeric and hyphens.

        Format: S-{L}-{timestamp_ms_short}-{seq:04d}[-{tag}]
        Example: S-C-173148123-0001
        """
        self._seq_counter += 1
        leg_code = "C" if leg_type == OptionLegType.CALL else "P"
        # 9-digit millisecond timestamp to guarantee uniqueness while conserving length
        ts_ms = int(time.time() * 1000) % 1_000_000_000
        seq = self._seq_counter % 10000

        base_id = f"S-{leg_code}-{ts_ms:09d}-{seq:04d}"
        if tag:
            clean_tag = "".join(c for c in tag if c.isalnum() or c == "-")[:6]
            client_id_str = f"{base_id}-{clean_tag}"[:36]
        else:
            client_id_str = base_id[:36]

        if client_id_str in self._seen_client_order_ids:
            # Handle millisecond collision with secondary salt
            client_id_str = f"{client_id_str[:30]}-{self._seq_counter:05d}"[:36]

        self._seen_client_order_ids.add(client_id_str)
        return ClientOrderId(client_id_str)

    # -------------------------------------------------------------------------
    # 2. Quantity Validation
    # -------------------------------------------------------------------------

    def validate_order_quantity(
        self,
        quantity: float,
        min_lot_size: float = 0.01,
        lot_size_step: float = 0.01,
        max_lot_size: Optional[float] = None,
    ) -> float:
        """
        Validate order quantity against exchange and contract specifications:
        1. Quantity must be strictly positive.
        2. Quantity must be >= min_lot_size (e.g. 0.01 BTC).
        3. Quantity must conform to lot_size_step increments.
        4. Quantity must not exceed max_lot_size if specified.
        """
        if quantity <= 0.0:
            raise InvalidQuantityError(f"Order quantity must be positive, got {quantity}")

        if quantity < (min_lot_size - 1e-9):
            raise InvalidQuantityError(
                f"Order quantity {quantity} is less than minimum lot size {min_lot_size}"
            )

        if max_lot_size is not None and quantity > (max_lot_size + 1e-9):
            raise InvalidQuantityError(
                f"Order quantity {quantity} exceeds maximum allowed lot size {max_lot_size}"
            )

        # Check step increments: remainder of (qty - min_qty) / step must be ~0
        if lot_size_step > 0:
            steps = round((quantity - min_lot_size) / lot_size_step, 6)
            if abs(steps - round(steps)) > 1e-4:
                raise InvalidQuantityError(
                    f"Order quantity {quantity} does not align with lot step {lot_size_step} "
                    f"(min: {min_lot_size})"
                )

        return round(quantity, 4)

    # -------------------------------------------------------------------------
    # 3. Order Creation (SELL Orders for Short Straddle Entry)
    # -------------------------------------------------------------------------

    def create_sell_order(
        self,
        instrument: Union[CryptoOption, Instrument],
        quantity: float,
        price: Optional[float] = None,
        leg_type: OptionLegType = OptionLegType.CALL,
        custom_client_order_id: Optional[ClientOrderId] = None,
        time_in_force: TimeInForce = TimeInForce.GTC,
    ) -> Tuple[Order, LegOrderTracker]:
        """
        Construct a NautilusTrader SELL Order (Limit if price is provided, Market otherwise)
        and wrap it in an independent LegOrderTracker.
        """
        # Validate quantity against instrument constraints
        min_qty = float(instrument.min_quantity) if hasattr(instrument, "min_quantity") and instrument.min_quantity else 0.01
        step_qty = float(instrument.size_increment) if hasattr(instrument, "size_increment") and instrument.size_increment else 0.01
        validated_qty = self.validate_order_quantity(quantity, min_lot_size=min_qty, lot_size_step=step_qty)

        # Assign unique client order ID
        client_order_id = custom_client_order_id or self.generate_client_order_id(leg_type)
        if client_order_id.value in self._orders_by_client_id:
            raise DuplicateOrderError(
                f"Order with client_order_id '{client_order_id.value}' already exists."
            )

        nautilus_qty = Quantity.from_str(f"{validated_qty:.{instrument.size_precision}f}")

        # Construct actual NautilusTrader Order
        if price is not None and price > 0.0:
            price_prec = getattr(instrument, "price_precision", 2)
            nautilus_px = Price.from_str(f"{price:.{price_prec}f}")
            order = self.order_factory.limit(
                instrument_id=instrument.id,
                order_side=OrderSide.SELL,
                quantity=nautilus_qty,
                price=nautilus_px,
                time_in_force=time_in_force,
                client_order_id=client_order_id,
            )
        else:
            order = self.order_factory.market(
                instrument_id=instrument.id,
                order_side=OrderSide.SELL,
                quantity=nautilus_qty,
                time_in_force=TimeInForce.GTC,
                client_order_id=client_order_id,
            )

        # Initialize LegOrderTracker
        tracker = LegOrderTracker(
            leg_type=leg_type,
            instrument_id=instrument.id,
            order_side=OrderSide.SELL,
            target_quantity=validated_qty,
            target_price=price,
            client_order_id=client_order_id,
            status=LegExecutionStatus.NOT_SUBMITTED,
            nautilus_order=order,
        )

        self._orders_by_client_id[client_order_id] = tracker
        return order, tracker

    def create_straddle_sell_orders(
        self,
        call_instrument: Union[CryptoOption, Instrument],
        put_instrument: Union[CryptoOption, Instrument],
        quantity: float,
        call_price: Optional[float] = None,
        put_price: Optional[float] = None,
        unmatched_policy: Optional[UnmatchedLegPolicy] = None,
    ) -> StraddleOrder:
        """
        Create a paired short straddle consisting of one Call SELL order
        and one Put SELL order sharing the exact same quantity.
        """
        # Guard: Check if an active straddle is already in flight
        if self._active_straddle is not None and not self._active_straddle.is_terminal:
            raise DuplicateOrderError(
                f"Cannot create new straddle: Active straddle '{self._active_straddle.straddle_id}' "
                f"is currently in flight with status {self._active_straddle.status.value}."
            )

        call_order, call_tracker = self.create_sell_order(
            instrument=call_instrument,
            quantity=quantity,
            price=call_price,
            leg_type=OptionLegType.CALL,
        )

        put_order, put_tracker = self.create_sell_order(
            instrument=put_instrument,
            quantity=quantity,
            price=put_price,
            leg_type=OptionLegType.PUT,
        )

        straddle_id = f"STRADDLE-{int(time.time())}-{self._seq_counter:03d}"
        straddle = StraddleOrder(
            straddle_id=straddle_id,
            call_leg=call_tracker,
            put_leg=put_tracker,
            unmatched_policy=unmatched_policy or self.unmatched_policy,
            status=StraddleExecutionStatus.NOT_SUBMITTED,
        )

        self._straddles.append(straddle)
        self._active_straddle = straddle

        logger.info(
            f"Created StraddleOrder: id={straddle_id}, "
            f"Call={call_instrument.id} (cid={call_tracker.client_order_id}), "
            f"Put={put_instrument.id} (cid={put_tracker.client_order_id}), "
            f"Quantity={quantity} BTC each"
        )
        return straddle

    # -------------------------------------------------------------------------
    # 4. Order Submission (DRY_RUN & Execution Client Handling)
    # -------------------------------------------------------------------------

    def submit_straddle(self, straddle: StraddleOrder) -> None:
        """
        Submit both legs of the straddle structure.
        In DRY_RUN mode: Transitions status to SUBMITTED without sending network packets.
        In LIVE/PAPER mode: Dispatches orders via the execution client or submit callback.
        """
        if straddle.status != StraddleExecutionStatus.NOT_SUBMITTED:
            raise DuplicateOrderError(
                f"Straddle '{straddle.straddle_id}' has already been submitted (status={straddle.status.value})."
            )

        logger.info(
            f"Submitting straddle '{straddle.straddle_id}' "
            f"[Mode: {'DRY_RUN (Simulated)' if self.dry_run else 'PAPER / LIVE'}]"
        )

        # Submit Call Leg
        self._submit_single_leg(straddle.call_leg)

        # Submit Put Leg
        self._submit_single_leg(straddle.put_leg)

        # Update composite status
        straddle.evaluate_composite_status()

    def _submit_single_leg(self, leg_tracker: LegOrderTracker) -> None:
        """Submit a single leg tracker using the configured execution client or dry-run path."""
        if leg_tracker.status != LegExecutionStatus.NOT_SUBMITTED:
            raise DuplicateOrderError(
                f"Leg order '{leg_tracker.client_order_id}' is already {leg_tracker.status.value}."
            )

        order = leg_tracker.nautilus_order
        if order is None:
            raise OrderSubmissionError("Nautilus Order instance missing in LegOrderTracker.")

        if self.dry_run:
            # DRY_RUN: Emulate local order submission event without live venue network calls
            logger.info(
                f"[DRY_RUN] Simulated submission of {leg_tracker.leg_type.value} leg "
                f"(cid={leg_tracker.client_order_id})"
            )
            sub_event = OrderSubmitted(
                trader_id=self.trader_id,
                strategy_id=self.strategy_id,
                instrument_id=leg_tracker.instrument_id,
                client_order_id=leg_tracker.client_order_id,
                account_id=self.account_id,
                event_id=UUID4(),
                ts_event=time.time_ns(),
                ts_init=time.time_ns(),
            )
            leg_tracker.apply_event(sub_event)

        else:
            # LIVE / PAPER: Send to actual venue via execution client or submit callback
            try:
                if self.submit_fn is not None:
                    self.submit_fn(order)
                elif self.execution_client is not None and hasattr(self.execution_client, "submit_order"):
                    # Check if client expects SubmitOrder command or raw order
                    from nautilus_trader.execution.messages import SubmitOrder

                    cmd = SubmitOrder(
                        trader_id=self.trader_id,
                        strategy_id=self.strategy_id,
                        order=order,
                        command_id=UUID4(),
                        ts_init=time.time_ns(),
                    )
                    self.execution_client.submit_order(cmd)
                else:
                    raise OrderSubmissionError(
                        "Execution client or submit callback is required when DRY_RUN=False."
                    )

                # Mark as submitted pending exchange response
                sub_event = OrderSubmitted(
                    trader_id=self.trader_id,
                    strategy_id=self.strategy_id,
                    instrument_id=leg_tracker.instrument_id,
                    client_order_id=leg_tracker.client_order_id,
                    account_id=self.account_id,
                    event_id=UUID4(),
                    ts_event=time.time_ns(),
                    ts_init=time.time_ns(),
                )
                leg_tracker.apply_event(sub_event)

            except Exception as e:
                logger.error(f"Failed to submit leg {leg_tracker.client_order_id}: {e}", exc_info=True)
                raise OrderSubmissionError(f"Submission failed for {leg_tracker.client_order_id}: {e}")

    # -------------------------------------------------------------------------
    # 5. Order Event Processing & State Machine Transitions
    # -------------------------------------------------------------------------

    def on_order_event(self, event: OrderEvent) -> None:
        """
        Generic entrypoint for routing NautilusTrader OrderEvents to their
        respective leg trackers and recomputing composite straddle state.
        """
        cid = event.client_order_id
        tracker = self._orders_by_client_id.get(cid)
        if not tracker:
            logger.debug(f"Received OrderEvent for unmanaged client_order_id={cid}")
            return

        # Apply event to leg tracker
        tracker.apply_event(event)

        # Re-evaluate parent straddle status if applicable
        if self._active_straddle is not None:
            prev_status = self._active_straddle.status
            new_status = self._active_straddle.evaluate_composite_status()

            if prev_status != new_status:
                logger.info(
                    f"Straddle '{self._active_straddle.straddle_id}' status transitioned: "
                    f"{prev_status.value} -> {new_status.value}"
                )

                # Trigger unmatched leg detection check if transitioned to ONE_LEG_FILLED
                if new_status == StraddleExecutionStatus.ONE_LEG_FILLED:
                    logger.warning(
                        f"CRITICAL: Unmatched leg detected in straddle '{self._active_straddle.straddle_id}'! "
                        f"Filled Leg: {self._active_straddle.unmatched_filled_leg.leg_type.value} "
                        f"(qty={self._active_straddle.unmatched_filled_leg.filled_quantity}), "
                        f"Pending Leg: {self._active_straddle.unmatched_pending_leg.leg_type.value} "
                        f"(status={self._active_straddle.unmatched_pending_leg.status.value})"
                    )

    def on_order_submitted(self, event: OrderSubmitted) -> None:
        """Callback for OrderSubmitted event."""
        self.on_order_event(event)

    def on_order_accepted(self, event: OrderAccepted) -> None:
        """Callback for OrderAccepted event."""
        self.on_order_event(event)

    def on_order_rejected(self, event: OrderRejected) -> None:
        """Callback for OrderRejected event."""
        self.on_order_event(event)

    def on_order_filled(self, event: OrderFilled) -> None:
        """Callback for OrderFilled event."""
        self.on_order_event(event)

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """Callback for OrderCanceled event."""
        self.on_order_event(event)

    # -------------------------------------------------------------------------
    # 6. Unmatched-Leg Detection & Policy Resolution
    # -------------------------------------------------------------------------

    def check_unmatched_leg(
        self,
        straddle: Optional[StraddleOrder] = None,
    ) -> Optional[Tuple[LegOrderTracker, LegOrderTracker]]:
        """
        Check if an unmatched leg condition exists.
        Returns (filled_leg, pending_leg) if exactly one leg is filled, else None.
        """
        target = straddle or self._active_straddle
        if target is None:
            return None

        if target.is_one_leg_filled:
            return target.unmatched_filled_leg, target.unmatched_pending_leg
        return None

    def resolve_unmatched_leg(
        self,
        straddle: Optional[StraddleOrder] = None,
        policy_override: Optional[UnmatchedLegPolicy] = None,
    ) -> Dict[str, Any]:
        """
        Execute the configured unmatched-leg policy when one leg has filled
        and the second leg has timed out or remains pending.

        Supported Policies:
        1. WAIT: Do nothing, continue awaiting second leg execution.
        2. CANCEL_REMAINING: Cancel the pending leg order; retain filled leg.
        3. CANCEL_AND_CLOSE (or CLOSE_FILLED):
           - Cancel the pending leg order immediately.
           - Create and submit an offsetting BUY order for the filled leg to
             neutralize exposure and buy back the short option.
        """
        target = straddle or self._active_straddle
        if target is None:
            raise UnmatchedLegError("No active straddle available to resolve.")

        unmatched = self.check_unmatched_leg(target)
        if not unmatched:
            logger.info("resolve_unmatched_leg called but no unmatched leg condition exists.")
            return {"status": "NO_UNMATCHED_LEG", "straddle_status": target.status.value}

        filled_leg, pending_leg = unmatched
        policy = policy_override or target.unmatched_policy

        logger.warning(
            f"Executing unmatched-leg resolution for straddle '{target.straddle_id}' "
            f"under policy '{policy.value}'. "
            f"Filled: {filled_leg.leg_type.value} ({filled_leg.filled_quantity} BTC), "
            f"Pending: {pending_leg.leg_type.value} ({pending_leg.status.value})"
        )

        result: Dict[str, Any] = {
            "policy": policy.value,
            "filled_leg": filled_leg.leg_type.value,
            "pending_leg": pending_leg.leg_type.value,
            "actions_taken": [],
        }

        if policy == UnmatchedLegPolicy.WAIT:
            logger.info(f"Policy is WAIT: Maintaining wait state for pending {pending_leg.leg_type.value} leg.")
            result["actions_taken"].append("AWAITING_SECOND_LEG")
            return result

        # For CANCEL_REMAINING and CANCEL_AND_CLOSE: Cancel the outstanding pending leg
        if pending_leg.is_active:
            self.cancel_leg_order(pending_leg)
            result["actions_taken"].append(f"CANCELED_{pending_leg.leg_type.value}_LEG")

        # For CANCEL_AND_CLOSE: Close out the filled leg by buying it back
        if policy in (UnmatchedLegPolicy.CANCEL_AND_CLOSE, UnmatchedLegPolicy.CLOSE_FILLED):
            close_tracker = self._close_filled_leg(filled_leg, target)
            result["actions_taken"].append(f"SUBMITTED_BUYBACK_FOR_{filled_leg.leg_type.value}")
            result["closing_order_id"] = str(close_tracker.client_order_id)
            target.status = StraddleExecutionStatus.RESOLVING_UNMATCHED

        return result

    def _close_filled_leg(
        self,
        filled_leg: LegOrderTracker,
        straddle: StraddleOrder,
    ) -> LegOrderTracker:
        """
        Create and submit an offsetting BUY order for the filled leg to close out the position.
        Uses Market Order (or aggressive bid) to ensure immediate execution.
        """
        qty = filled_leg.filled_quantity
        client_order_id = self.generate_client_order_id(filled_leg.leg_type, tag="CLOSE")

        nautilus_qty = Quantity.from_str(f"{qty:.4f}")

        # Construct offsetting BUY market order
        close_order = self.order_factory.market(
            instrument_id=filled_leg.instrument_id,
            order_side=OrderSide.BUY,
            quantity=nautilus_qty,
            time_in_force=TimeInForce.GTC,
            client_order_id=client_order_id,
        )

        close_tracker = LegOrderTracker(
            leg_type=filled_leg.leg_type,
            instrument_id=filled_leg.instrument_id,
            order_side=OrderSide.BUY,
            target_quantity=qty,
            client_order_id=client_order_id,
            status=LegExecutionStatus.NOT_SUBMITTED,
            nautilus_order=close_order,
        )

        self._orders_by_client_id[client_order_id] = close_tracker

        if filled_leg.leg_type == OptionLegType.CALL:
            straddle.closing_call_order = close_tracker
        else:
            straddle.closing_put_order = close_tracker

        # Submit the closing order
        self._submit_single_leg(close_tracker)

        logger.info(
            f"Offsetting BUY order submitted for {filled_leg.leg_type.value} leg: "
            f"cid={client_order_id}, qty={qty} BTC"
        )
        return close_tracker

    # -------------------------------------------------------------------------
    # 7. Cancellation Interface
    # -------------------------------------------------------------------------

    def cancel_leg_order(self, leg_tracker: LegOrderTracker) -> None:
        """Cancel an individual leg order."""
        if leg_tracker.is_terminal:
            logger.debug(f"Leg order {leg_tracker.client_order_id} already terminal; skipping cancel.")
            return

        cid = leg_tracker.client_order_id
        if cid is None:
            return

        logger.info(f"Canceling leg order: cid={cid}")

        if self.dry_run:
            # Emulate cancellation event locally
            can_event = OrderCanceled(
                trader_id=self.trader_id,
                strategy_id=self.strategy_id,
                instrument_id=leg_tracker.instrument_id,
                client_order_id=cid,
                venue_order_id=leg_tracker.venue_order_id,
                account_id=self.account_id,
                event_id=UUID4(),
                ts_event=time.time_ns(),
                ts_init=time.time_ns(),
            )
            self.on_order_canceled(can_event)

        else:
            if self.cancel_fn is not None:
                self.cancel_fn(cid)
            elif self.execution_client is not None and hasattr(self.execution_client, "cancel_order"):
                from nautilus_trader.execution.messages import CancelOrder

                cmd = CancelOrder(
                    trader_id=self.trader_id,
                    strategy_id=self.strategy_id,
                    instrument_id=leg_tracker.instrument_id,
                    client_order_id=cid,
                    venue_order_id=leg_tracker.venue_order_id,
                    command_id=UUID4(),
                    ts_init=time.time_ns(),
                )
                self.execution_client.cancel_order(cmd)
            else:
                raise OrderManagementError("No cancel handler configured for live execution.")

    def cancel_straddle(self, straddle: Optional[StraddleOrder] = None) -> None:
        """Cancel both legs of a straddle structure."""
        target = straddle or self._active_straddle
        if target is None:
            return

        logger.info(f"Canceling straddle '{target.straddle_id}'")
        if target.call_leg.is_active:
            self.cancel_leg_order(target.call_leg)
        if target.put_leg.is_active:
            self.cancel_leg_order(target.put_leg)

        target.evaluate_composite_status()

    # -------------------------------------------------------------------------
    # 8. Testing & Simulation Helpers
    # -------------------------------------------------------------------------

    def simulate_acceptance(
        self,
        client_order_id: ClientOrderId,
        venue_order_id: Optional[str] = None,
    ) -> OrderAccepted:
        """Simulate venue acceptance event for testing and dry-run flows."""
        tracker = self._orders_by_client_id.get(client_order_id)
        if not tracker:
            raise OrderManagementError(f"Unknown client_order_id: {client_order_id}")

        v_id = VenueOrderId(venue_order_id or f"V-{client_order_id.value}")
        event = OrderAccepted(
            trader_id=self.trader_id,
            strategy_id=self.strategy_id,
            instrument_id=tracker.instrument_id,
            client_order_id=client_order_id,
            venue_order_id=v_id,
            account_id=self.account_id,
            event_id=UUID4(),
            ts_event=time.time_ns(),
            ts_init=time.time_ns(),
        )
        self.on_order_accepted(event)
        return event

    def simulate_fill(
        self,
        client_order_id: ClientOrderId,
        fill_quantity: float,
        fill_price: float,
        trade_id: Optional[str] = None,
        commission: float = 0.05,
    ) -> OrderFilled:
        """Simulate execution fill event for testing and dry-run flows."""
        tracker = self._orders_by_client_id.get(client_order_id)
        if not tracker:
            raise OrderManagementError(f"Unknown client_order_id: {client_order_id}")

        t_id = TradeId(trade_id or f"T-{int(time.time()*1000)}-{len(tracker.fills)+1}")
        v_id = tracker.venue_order_id or VenueOrderId(f"V-{client_order_id.value}")

        event = OrderFilled(
            trader_id=self.trader_id,
            strategy_id=self.strategy_id,
            instrument_id=tracker.instrument_id,
            client_order_id=client_order_id,
            venue_order_id=v_id,
            account_id=self.account_id,
            trade_id=t_id,
            position_id=None,
            order_side=tracker.order_side,
            order_type=OrderType.LIMIT if tracker.target_price else OrderType.MARKET,
            last_qty=Quantity.from_str(f"{fill_quantity:.4f}"),
            last_px=Price.from_str(f"{fill_price:.2f}"),
            currency=Currency.from_str("USDT"),
            commission=Money.from_str(f"{commission:.2f} USDT"),
            liquidity_side=LiquiditySide.MAKER,
            event_id=UUID4(),
            ts_event=time.time_ns(),
            ts_init=time.time_ns(),
        )
        self.on_order_filled(event)
        return event

    def simulate_rejection(
        self,
        client_order_id: ClientOrderId,
        reason: str = "Test simulated rejection",
    ) -> OrderRejected:
        """Simulate venue rejection event for testing and dry-run flows."""
        tracker = self._orders_by_client_id.get(client_order_id)
        if not tracker:
            raise OrderManagementError(f"Unknown client_order_id: {client_order_id}")

        event = OrderRejected(
            trader_id=self.trader_id,
            strategy_id=self.strategy_id,
            instrument_id=tracker.instrument_id,
            client_order_id=client_order_id,
            account_id=self.account_id,
            reason=reason,
            event_id=UUID4(),
            ts_event=time.time_ns(),
            ts_init=time.time_ns(),
        )
        self.on_order_rejected(event)
        return event

    def simulate_cancellation(
        self,
        client_order_id: ClientOrderId,
    ) -> OrderCanceled:
        """Simulate venue cancellation event for testing and dry-run flows."""
        tracker = self._orders_by_client_id.get(client_order_id)
        if not tracker:
            raise OrderManagementError(f"Unknown client_order_id: {client_order_id}")

        event = OrderCanceled(
            trader_id=self.trader_id,
            strategy_id=self.strategy_id,
            instrument_id=tracker.instrument_id,
            client_order_id=client_order_id,
            venue_order_id=tracker.venue_order_id,
            account_id=self.account_id,
            event_id=UUID4(),
            ts_event=time.time_ns(),
            ts_init=time.time_ns(),
        )
        self.on_order_canceled(event)
        return event

    # -------------------------------------------------------------------------
    # 9. Query & State Access
    # -------------------------------------------------------------------------

    def get_active_straddle(self) -> Optional[StraddleOrder]:
        """Return the current active straddle structure if one exists."""
        return self._active_straddle

    def get_leg_tracker(self, client_order_id: ClientOrderId) -> Optional[LegOrderTracker]:
        """Look up an individual leg tracker by ClientOrderId."""
        return self._orders_by_client_id.get(client_order_id)

    def get_all_straddles(self) -> List[StraddleOrder]:
        """Return all tracked straddle structures."""
        return list(self._straddles)

    def reset(self) -> None:
        """Clear all active orders and state registries (primarily for test resets)."""
        self._active_straddle = None
        self._straddles.clear()
        self._orders_by_client_id.clear()
        self._seen_client_order_ids.clear()
        self._seq_counter = 0
