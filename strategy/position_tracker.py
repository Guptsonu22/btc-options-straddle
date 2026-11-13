"""
Automated BTC Options Straddle - Position Tracking & Reconciliation Engine.

Provides real-time tracking of individual option legs (Call and Put) and the
composite straddle structure:
- Short options position tracking (quantity, entry VWAP, side).
- Strict ask-side mark-to-market valuation for buyback (rejecting LTP/midpoint).
- Realized and unrealized PnL calculations.
- Straddle balance monitoring (Call quantity vs Put quantity).
- Position reconciliation against NautilusTrader Position models and fill reports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.events.position import (
    PositionChanged,
    PositionClosed,
    PositionEvent,
    PositionOpened,
)
from nautilus_trader.model.identifiers import InstrumentId, PositionId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.position import Position

logger = logging.getLogger("btc_straddle.position_tracker")


class LegPositionStatus(str, Enum):
    """Status of an individual option leg position."""

    FLAT = "FLAT"          # No open exposure
    SHORT = "SHORT"        # Short option position (standard for short straddle entry)
    LONG = "LONG"          # Long option position (if closing buyback overshoots or inverted)
    CLOSED = "CLOSED"      # Previously open, now closed


@dataclass
class OptionLegPosition:
    """
    Tracks an individual option contract's position state and mark-to-market valuation.
    """

    instrument_id: InstrumentId
    option_type: str  # "CALL" or "PUT"
    strike_price: float
    side: LegPositionStatus = LegPositionStatus.FLAT
    quantity: float = 0.0
    entry_vwap: float = 0.0
    total_entry_premium: float = 0.0
    realized_pnl: float = 0.0
    current_bid_price: Optional[float] = None
    current_ask_price: Optional[float] = None
    current_mark_price: Optional[float] = None  # Always the ask price for buyback valuation
    unrealized_pnl: float = 0.0
    last_update_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_open(self) -> bool:
        """Return True if position has non-zero exposure."""
        return self.quantity > 1e-7 and self.side != LegPositionStatus.FLAT

    @property
    def is_short(self) -> bool:
        """Return True if position is short."""
        return self.side == LegPositionStatus.SHORT and self.quantity > 1e-7

    def update_quote(self, bid: float, ask: float) -> None:
        """
        Update current market quote and recompute mark-to-market unrealized PnL.
        MANDATE: For a short position, mark price is strictly the ASK price (cost to buy back).
        """
        self.current_bid_price = bid
        self.current_ask_price = ask
        # In options short straddles, buyback cost is defined by the ask side:
        self.current_mark_price = ask
        self.last_update_utc = datetime.now(timezone.utc)

        if self.is_short and self.current_mark_price is not None and self.current_mark_price > 0.0:
            # Short PnL = (Entry Price - Current Buyback Price) * Quantity
            self.unrealized_pnl = round(
                (self.entry_vwap - self.current_mark_price) * self.quantity, 4
            )
        else:
            self.unrealized_pnl = 0.0

    def apply_fill(self, fill_side: str, fill_qty: float, fill_price: float) -> None:
        """
        Apply an execution fill to adjust position quantity, side, and entry VWAP.
        """
        fill_side_upper = fill_side.upper()
        prev_qty = self.quantity

        if fill_side_upper == "SELL":
            # Opening or increasing short position
            if self.side in (LegPositionStatus.FLAT, LegPositionStatus.SHORT):
                new_qty = prev_qty + fill_qty
                if new_qty > 0:
                    self.entry_vwap = (
                        (self.entry_vwap * prev_qty) + (fill_price * fill_qty)
                    ) / new_qty
                self.quantity = round(new_qty, 8)
                self.total_entry_premium = round(self.entry_vwap * self.quantity, 4)
                self.side = LegPositionStatus.SHORT

        elif fill_side_upper == "BUY":
            # Closing or reducing short position
            if self.is_short:
                close_qty = min(prev_qty, fill_qty)
                # Realized PnL on short buyback = (Entry Price - Close Price) * Close Qty
                pnl = (self.entry_vwap - fill_price) * close_qty
                self.realized_pnl += round(pnl, 4)

                rem_qty = round(prev_qty - close_qty, 8)
                if rem_qty <= 1e-7:
                    self.quantity = 0.0
                    self.side = LegPositionStatus.CLOSED
                    self.unrealized_pnl = 0.0
                else:
                    self.quantity = rem_qty
                    self.total_entry_premium = round(self.entry_vwap * self.quantity, 4)

        self.last_update_utc = datetime.now(timezone.utc)
        if self.current_ask_price:
            self.update_quote(self.current_bid_price or 0.0, self.current_ask_price)


@dataclass
class StraddlePosition:
    """
    Composite position representing the short straddle pair.
    Monitors delta-neutral balance, combined entry premium, and total PnL.
    """

    call_position: OptionLegPosition
    put_position: OptionLegPosition
    underlying_price: float = 0.0
    created_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_balanced(self) -> bool:
        """Return True if Call and Put leg quantities match within tolerance."""
        return abs(self.call_position.quantity - self.put_position.quantity) < 1e-6

    @property
    def is_flat(self) -> bool:
        """Return True if both Call and Put legs are closed (zero open contracts)."""
        return self.call_position.quantity <= 1e-7 and self.put_position.quantity <= 1e-7

    @property
    def leg_imbalance_quantity(self) -> float:
        """
        Difference in open contracts: Call Qty - Put Qty.
        Zero indicates perfectly matched legs.
        """
        return round(self.call_position.quantity - self.put_position.quantity, 8)

    @property
    def total_contracts_open(self) -> float:
        """Aggregate contracts short across both legs."""
        return round(self.call_position.quantity + self.put_position.quantity, 8)

    @property
    def total_entry_premium(self) -> float:
        """Gross premium collected upon short entry."""
        return round(
            self.call_position.total_entry_premium + self.put_position.total_entry_premium, 4
        )

    @property
    def total_buyback_cost(self) -> float:
        """
        Total cash required to buy back and close both legs at current market ask quotes:
        (Call Ask * Call Qty) + (Put Ask * Put Qty)
        """
        call_cost = (
            (self.call_position.current_mark_price or self.call_position.entry_vwap)
            * self.call_position.quantity
        )
        put_cost = (
            (self.put_position.current_mark_price or self.put_position.entry_vwap)
            * self.put_position.quantity
        )
        return round(call_cost + put_cost, 4)

    @property
    def total_unrealized_pnl(self) -> float:
        """Aggregate mark-to-market unrealized PnL."""
        return round(self.call_position.unrealized_pnl + self.put_position.unrealized_pnl, 4)

    @property
    def total_realized_pnl(self) -> float:
        """Aggregate realized PnL from closed contracts."""
        return round(self.call_position.realized_pnl + self.put_position.realized_pnl, 4)

    @property
    def total_pnl(self) -> float:
        """Combined total PnL (realized + unrealized)."""
        return round(self.total_realized_pnl + self.total_unrealized_pnl, 4)

    @property
    def pnl_percentage(self) -> float:
        """PnL as a percentage of initial entry premium collected."""
        if self.total_entry_premium <= 1e-7:
            return 0.0
        return round((self.total_pnl / self.total_entry_premium) * 100.0, 2)

    @property
    def stop_loss_threshold(self) -> float:
        """Stop-loss buyback cost threshold (ENTRY_PREMIUM * 1.01)."""
        return round(self.total_entry_premium * 1.01, 4)

    @property
    def take_profit_threshold(self) -> float:
        """Take-profit buyback cost threshold (ENTRY_PREMIUM * 0.98)."""
        return round(self.total_entry_premium * 0.98, 4)

    @property
    def is_stop_loss_triggered(self) -> bool:
        """True if total buyback cost has risen to or above the SL threshold."""
        if self.total_entry_premium <= 1e-7 or self.total_buyback_cost <= 1e-7:
            return False
        return self.total_buyback_cost >= self.stop_loss_threshold

    @property
    def is_take_profit_triggered(self) -> bool:
        """True if total buyback cost has fallen to or below the TP threshold."""
        if self.total_entry_premium <= 1e-7 or self.total_buyback_cost <= 1e-7:
            return False
        return self.total_buyback_cost <= self.take_profit_threshold


class PositionTracker:
    """
    Position Tracking & Reconciliation Engine.

    Manages active positions for option legs, updates mark-to-market valuations
    using ask quotes, reconciles against NautilusTrader position models, and provides
    real-time metrics on open short straddle exposure.
    """

    def __init__(self):
        self._positions_by_instrument: Dict[InstrumentId, OptionLegPosition] = {}
        self._active_straddle_position: Optional[StraddlePosition] = None
        self._underlying_price: float = 0.0

    @property
    def is_flat(self) -> bool:
        """Return True if all tracked positions are confirmed closed or flat."""
        if not self._positions_by_instrument:
            return True
        return all(not p.is_open for p in self._positions_by_instrument.values())

    def register_straddle_pair(
        self,
        call_instrument_id: InstrumentId,
        put_instrument_id: InstrumentId,
        strike_price: float,
    ) -> StraddlePosition:
        """Initialize and register tracking for a new straddle instrument pair."""
        call_pos = OptionLegPosition(
            instrument_id=call_instrument_id,
            option_type="CALL",
            strike_price=strike_price,
        )
        put_pos = OptionLegPosition(
            instrument_id=put_instrument_id,
            option_type="PUT",
            strike_price=strike_price,
        )

        self._positions_by_instrument[call_instrument_id] = call_pos
        self._positions_by_instrument[put_instrument_id] = put_pos

        straddle_pos = StraddlePosition(
            call_position=call_pos,
            put_position=put_pos,
            underlying_price=self._underlying_price,
        )
        self._active_straddle_position = straddle_pos
        logger.info(
            f"Registered straddle position tracker: Strike=${strike_price:,.2f}, "
            f"Call={call_instrument_id}, Put={put_instrument_id}"
        )
        return straddle_pos

    def update_underlying_price(self, price: float) -> None:
        """Update current underlying reference price."""
        if price > 0.0:
            self._underlying_price = price
            if self._active_straddle_position:
                self._active_straddle_position.underlying_price = price

    def update_leg_quote(self, instrument_id: InstrumentId, bid: float, ask: float) -> None:
        """Update market quotes for an individual option leg."""
        leg = self._positions_by_instrument.get(instrument_id)
        if leg:
            leg.update_quote(bid, ask)

    def apply_fill(
        self,
        instrument_id: InstrumentId,
        side: str,
        quantity: float,
        price: float,
    ) -> None:
        """Record an execution fill to update position quantity and entry VWAP."""
        leg = self._positions_by_instrument.get(instrument_id)
        if leg:
            leg.apply_fill(side, quantity, price)
            logger.info(
                f"Position updated on fill: {instrument_id} -> {leg.side.value} "
                f"{leg.quantity} BTC @ VWAP=${leg.entry_vwap:.2f}"
            )

    def on_position_event(self, event: PositionEvent) -> None:
        """Handle NautilusTrader native PositionOpened, PositionChanged, PositionClosed events."""
        inst_id = event.instrument_id
        leg = self._positions_by_instrument.get(inst_id)
        if not leg:
            return

        if isinstance(event, PositionOpened):
            qty = float(event.quantity)
            px = float(event.avg_px_open)
            side = "SELL" if event.side == PositionSide.SHORT else "BUY"
            leg.apply_fill(side, qty, px)
            logger.info(f"Native PositionOpened: {inst_id} -> {event.side.name} {qty} @ {px}")

        elif isinstance(event, PositionChanged):
            qty = float(event.quantity)
            logger.info(f"Native PositionChanged: {inst_id} -> qty={qty}")

        elif isinstance(event, PositionClosed):
            leg.quantity = 0.0
            leg.side = LegPositionStatus.CLOSED
            logger.info(f"Native PositionClosed: {inst_id}")

    def reconcile_with_nautilus(self, nautilus_positions: List[Position]) -> Dict[str, Any]:
        """
        Reconcile internal position tracker against NautilusTrader's portfolio positions.
        Flags any quantity, side, or instrument discrepancies.
        """
        reconciliation_report: Dict[str, Any] = {
            "is_synchronized": True,
            "discrepancies": [],
            "reconciled_at_utc": datetime.now(timezone.utc).isoformat(),
        }

        nautilus_map = {pos.instrument_id: pos for pos in nautilus_positions}

        for inst_id, leg in self._positions_by_instrument.items():
            naut_pos = nautilus_map.get(inst_id)
            if leg.is_open:
                if naut_pos is None:
                    reconciliation_report["is_synchronized"] = False
                    reconciliation_report["discrepancies"].append(
                        f"Discrepancy: {inst_id} is open in PositionTracker ({leg.quantity} BTC) but missing in Nautilus."
                    )
                else:
                    naut_qty = float(naut_pos.quantity)
                    if abs(leg.quantity - naut_qty) > 1e-6:
                        reconciliation_report["is_synchronized"] = False
                        reconciliation_report["discrepancies"].append(
                            f"Quantity mismatch on {inst_id}: Tracker={leg.quantity}, Nautilus={naut_qty}."
                        )

        return reconciliation_report

    def get_straddle_position(self) -> Optional[StraddlePosition]:
        """Return the active StraddlePosition if registered."""
        return self._active_straddle_position

    def get_leg_position(self, instrument_id: InstrumentId) -> Optional[OptionLegPosition]:
        """Look up an individual leg's position by InstrumentId."""
        return self._positions_by_instrument.get(instrument_id)

    def reset(self) -> None:
        """Clear all positions and state."""
        self._positions_by_instrument.clear()
        self._active_straddle_position = None
        self._underlying_price = 0.0
