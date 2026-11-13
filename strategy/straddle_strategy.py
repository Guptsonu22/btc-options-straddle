"""
Automated BTC Options Straddle - Level 1 Strategy Implementation.

Integrates:
1. NautilusTrader Strategy lifecycle (on_start, on_quote_tick, callbacks, on_stop).
2. Bybit options instrument discovery and reference price ingestion.
3. Nearest ATM strike and Call/Put pair selection.
4. Real-time market data subscriptions and quote freshness validation.
5. Order quantity validation and short straddle SELL order creation.
6. Order submission, acceptance, fill, rejection, and cancellation tracking.
7. Independent leg tracking and partial-fill handling via OrderManager.
8. Position tracking, reconciliation, and continuous market movement monitoring.
9. Critical scenario handling: Call filled, Put pending -> unmatched-leg resolution.
10. Strict DRY_RUN safety and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from nautilus_trader.common.component import Clock, LiveClock, TestClock
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    OptionKind,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
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
from nautilus_trader.model.events.position import (
    PositionChanged,
    PositionClosed,
    PositionEvent,
    PositionOpened,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    PositionId,
    StrategyId,
    Symbol,
    TraderId,
    Venue,
    VenueOrderId,
)
from nautilus_trader.model.instruments import CryptoOption, Instrument
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from exchange.instrument_discovery import (
    BtcOptionsDiscoveryService,
    DiscoveredOptionsChain,
    OptionContractDetails,
)
from exchange.market_data import MarketDataManager, ValidatedQuote
from strategy.instrument_selector import (
    ExpirySelectionPolicy,
    InstrumentSelector,
    SelectedStraddleLegs,
)
from strategy.order_manager import (
    LegExecutionStatus,
    OptionLegType,
    OrderManager,
    StraddleExecutionStatus,
    StraddleOrder,
    UnmatchedLegPolicy,
)
from strategy.position_tracker import PositionTracker, StraddlePosition

logger = logging.getLogger("btc_straddle.strategy")


# -----------------------------------------------------------------------------
# Strategy Lifecycle States
# -----------------------------------------------------------------------------


class StraddleStrategyState(str, Enum):
    """Internal finite state machine for the Level 1 Straddle Strategy."""

    INITIALIZING = "INITIALIZING"              # Node/Strategy starting up
    AWAITING_INSTRUMENTS = "AWAITING_INSTRUMENTS"  # Loading options chain
    AWAITING_UNDERLYING_PRICE = "AWAITING_UNDERLYING_PRICE"  # Ingesting BTC spot reference
    SELECTING_ATM_PAIR = "SELECTING_ATM_PAIR"  # Finding ATM strike
    SUBSCRIBING_MARKET_DATA = "SUBSCRIBING_MARKET_DATA"  # Subscribed to option quotes
    VALIDATING_QUOTES = "VALIDATING_QUOTES"    # Checking quote freshness and spreads
    READY_TO_ENTER = "READY_TO_ENTER"          # Pre-trade checks passed
    SUBMITTING_ORDERS = "SUBMITTING_ORDERS"    # Submitting short Call and Put orders
    AWAITING_FILLS = "AWAITING_FILLS"          # Orders accepted, waiting executions
    POSITION_ACTIVE = "POSITION_ACTIVE"        # Both legs short; monitoring position
    UNMATCHED_RECOVERING = "UNMATCHED_RECOVERING"  # One leg filled; resolving orphan leg
    STOPPED = "STOPPED"                        # Clean shutdown completed


# -----------------------------------------------------------------------------
# Strategy Configuration
# -----------------------------------------------------------------------------


class BtcShortStraddleStrategyConfig(StrategyConfig, frozen=True):
    """
    Configuration model for BtcShortStraddleStrategy.
    """

    btc_symbol: str = "BTC"
    underlying_instrument_id: str = "BTCUSDT-LINEAR.BYBIT"
    order_quantity: float = 0.01
    dry_run: bool = True
    max_quote_age_seconds: float = 5.0
    entry_timeout_seconds: float = 10.0
    unmatched_leg_policy: str = "CANCEL_AND_CLOSE"
    min_dte_hours: float = 2.0


# -----------------------------------------------------------------------------
# Strategy Implementation
# -----------------------------------------------------------------------------


class BtcShortStraddleStrategy(Strategy):
    """
    Production Level 1 Automated BTC Short Straddle Trading Strategy.

    Execution Flow:
    1. Ingests BTC underlying reference price (BTCUSDT-LINEAR.BYBIT).
    2. Identifies the nearest ATM strike price for the nearest valid expiration.
    3. Selects matching Call and Put contracts sharing the identical strike and expiry.
    4. Subscribes to real-time QuoteTicks for both option contracts.
    5. Validates quote freshness, positive bid/ask prices, and spread sanity.
    6. Validates order quantities against exchange lot size and step constraints.
    7. Creates and submits SELL orders for both Call and Put legs.
    8. Tracks order submissions, venue acknowledgements, and executions.
    9. Monitors fills and calculates volume-weighted average entry prices (VWAP).
    10. Recognizes the critical orphan scenario (Call filled, Put pending) and executes
        the configured unmatched-leg policy (canceling pending leg and buying back filled leg).
    11. Reconciles and tracks net short positions and unrealized PnL continuously.
    12. Ensures graceful shutdown on stop.
    """

    def __init__(self, config: BtcShortStraddleStrategyConfig):
        super().__init__(config=config)

        # Domain components
        self.discovery_service = BtcOptionsDiscoveryService()
        self.instrument_selector = InstrumentSelector()
        self.market_data_manager = MarketDataManager(
            max_quote_age_seconds=config.max_quote_age_seconds
        )
        self.position_tracker = PositionTracker()

        # OrderManager instantiated during on_start when trader_id and order_factory are bound
        self.order_manager: Optional[OrderManager] = None

        # State tracking
        self.strategy_state: StraddleStrategyState = StraddleStrategyState.INITIALIZING
        self.underlying_id: InstrumentId = InstrumentId.from_str(config.underlying_instrument_id)
        self.options_chain: Optional[DiscoveredOptionsChain] = None
        self.selected_straddle: Optional[SelectedStraddleLegs] = None
        self.call_instrument_id: Optional[InstrumentId] = None
        self.put_instrument_id: Optional[InstrumentId] = None
        self.straddle_order: Optional[StraddleOrder] = None

        # Metrics & timestamps
        self.underlying_price: float = 0.0
        self.orders_submitted: bool = False
        self.entry_start_time_ns: Optional[int] = None

        logger.info(
            f"BtcShortStraddleStrategy initialized: underlying={self.underlying_id}, "
            f"order_quantity={self.config.order_quantity} BTC, dry_run={self.config.dry_run}, "
            f"unmatched_policy={self.config.unmatched_leg_policy}"
        )

    # -------------------------------------------------------------------------
    # NautilusTrader Strategy Lifecycle Callbacks
    # -------------------------------------------------------------------------

    def on_start(self) -> None:
        """
        Invoked when the strategy starts.
        Initializes OrderManager, subscribes to the underlying reference price,
        and triggers options chain discovery.
        """
        logger.info("[on_start] Starting BtcShortStraddleStrategy...")
        self.strategy_state = StraddleStrategyState.AWAITING_INSTRUMENTS

        # Determine trader ID, strategy ID, and clock (defaults if unregistered in unit tests)
        tid = getattr(self, "trader_id", None) or TraderId("STRADDLE-TRADER")
        sid = getattr(self, "id", None) or StrategyId("BTC-STRADDLE-001")
        clk = getattr(self, "clock", None) or LiveClock()

        # Initialize OrderManager wired to NautilusTrader order factory & strategy submission
        self.order_manager = OrderManager(
            trader_id=tid,
            strategy_id=sid,
            account_id=AccountId("BYBIT-TESTNET-001"),
            submit_fn=self._submit_nautilus_order if not self.config.dry_run else None,
            cancel_fn=self._cancel_nautilus_order if not self.config.dry_run else None,
            unmatched_policy=self.config.unmatched_leg_policy,
            dry_run=self.config.dry_run,
            clock=clk,
        )

        # 1. Subscribe to underlying reference quote if msgbus is registered
        if self._msgbus is not None:
            logger.info(f"[on_start] Subscribing to underlying: {self.underlying_id}")
            self.subscribe_quote_ticks(self.underlying_id)

        # 2. Trigger async discovery of active options chain if running in event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self._async_load_options_chain())
        except Exception as e:
            logger.debug(f"Async loop not active in on_start: {e}")

    def on_stop(self) -> None:
        """
        Invoked when the strategy stops.
        Unsubscribes from market data and ensures clean shutdown.
        """
        logger.info("[on_stop] Stopping BtcShortStraddleStrategy...")
        self.strategy_state = StraddleStrategyState.STOPPED

        # Unsubscribe if registered
        if self._msgbus is not None:
            try:
                self.unsubscribe_quote_ticks(self.underlying_id)
            except Exception:
                pass

            if self.call_instrument_id:
                try:
                    self.unsubscribe_quote_ticks(self.call_instrument_id)
                except Exception:
                    pass
            if self.put_instrument_id:
                try:
                    self.unsubscribe_quote_ticks(self.put_instrument_id)
                except Exception:
                    pass

        logger.info("[on_stop] Strategy stopped cleanly. Final position summary:")
        straddle_pos = self.position_tracker.get_straddle_position()
        if straddle_pos:
            logger.info(
                f"  - Contracts Open: Call={straddle_pos.call_position.quantity} BTC, "
                f"Put={straddle_pos.put_position.quantity} BTC"
            )
            logger.info(
                f"  - Net Entry Premium: ${straddle_pos.total_entry_premium:.2f} USDT"
            )
            logger.info(
                f"  - Total PnL: ${straddle_pos.total_pnl:.2f} USDT ({straddle_pos.pnl_percentage}%)"
            )

    # -------------------------------------------------------------------------
    # Market Data Handlers
    # -------------------------------------------------------------------------

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """
        Process incoming QuoteTick events for underlying and option legs.
        Drives the strategy state machine forward.
        """
        inst_id = tick.instrument_id
        bid_px = float(tick.bid_price)
        ask_px = float(tick.ask_price)

        # 1. Ingest Underlying Reference Price
        if inst_id == self.underlying_id:
            if bid_px > 0.0 and ask_px > 0.0:
                midpoint = round((bid_px + ask_px) / 2.0, 2)
                self.underlying_price = midpoint
                self.position_tracker.update_underlying_price(midpoint)
                self.market_data_manager.validate_and_ingest_quote(tick)

                # If awaiting underlying price, step to ATM selection
                if self.strategy_state in (
                    StraddleStrategyState.AWAITING_UNDERLYING_PRICE,
                    StraddleStrategyState.AWAITING_INSTRUMENTS,
                ) and self.options_chain is not None:
                    self._perform_atm_selection()
            return

        # 2. Ingest Option Leg Quotes
        if inst_id in (self.call_instrument_id, self.put_instrument_id):
            self.market_data_manager.validate_and_ingest_quote(tick)
            self.position_tracker.update_leg_quote(inst_id, bid_px, ask_px)

            # Check if both quotes are ready and valid for straddle entry
            if self.strategy_state == StraddleStrategyState.VALIDATING_QUOTES and not self.orders_submitted:
                self._check_quotes_and_submit_straddle()

    # -------------------------------------------------------------------------
    # ATM Selection & Market Data Subscription
    # -------------------------------------------------------------------------

    async def _async_load_options_chain(self) -> None:
        """Asynchronously load the active Bybit BTC options chain."""
        logger.info("Loading active BTC options chain from Bybit Testnet...")
        chain = await self.discovery_service.discover_btc_options()
        self.set_options_chain(chain)

    def set_options_chain(self, chain: DiscoveredOptionsChain) -> None:
        """Register the options chain and advance state machine."""
        self.options_chain = chain
        logger.info(
            f"Options chain loaded: {chain.total_active} active contracts "
            f"across {len(chain.expirations)} expirations."
        )
        if self.underlying_price > 0.0:
            self._perform_atm_selection()
        else:
            self.strategy_state = StraddleStrategyState.AWAITING_UNDERLYING_PRICE

    def _perform_atm_selection(self) -> None:
        """Calculate and select the nearest ATM strike Call and Put pair."""
        if not self.options_chain or self.underlying_price <= 0.0:
            return

        self.strategy_state = StraddleStrategyState.SELECTING_ATM_PAIR
        logger.info(
            f"Selecting ATM straddle pair for reference price ${self.underlying_price:,.2f}..."
        )

        try:
            selection = self.instrument_selector.select_straddle_pair(
                chain=self.options_chain,
                spot_price=self.underlying_price,
            )
            self.selected_straddle = selection
            self.call_instrument_id = InstrumentId.from_str(selection.call.instrument_id)
            self.put_instrument_id = InstrumentId.from_str(selection.put.instrument_id)

            logger.info(
                f"ATM Straddle Pair Selected: Strike=${selection.strike_price:,.2f}, "
                f"Expiry={selection.expiration_utc.strftime('%Y-%m-%d %H:%M UTC')} "
                f"({selection.time_to_expiry_days:.2f} DTE)\n"
                f"  Call: {self.call_instrument_id}\n"
                f"  Put:  {self.put_instrument_id}"
            )

            # Register pair with position tracker
            self.position_tracker.register_straddle_pair(
                call_instrument_id=self.call_instrument_id,
                put_instrument_id=self.put_instrument_id,
                strike_price=selection.strike_price,
            )

            # Subscribe to real-time quotes for both legs
            self.strategy_state = StraddleStrategyState.SUBSCRIBING_MARKET_DATA
            if self._msgbus is not None:
                logger.info(f"Subscribing to Call quote: {self.call_instrument_id}")
                self.subscribe_quote_ticks(self.call_instrument_id)
                logger.info(f"Subscribing to Put quote:  {self.put_instrument_id}")
                self.subscribe_quote_ticks(self.put_instrument_id)

            self.strategy_state = StraddleStrategyState.VALIDATING_QUOTES

        except Exception as e:
            logger.error(f"Failed to select ATM straddle pair: {e}", exc_info=True)

    # -------------------------------------------------------------------------
    # Quote Validation & Straddle Entry
    # -------------------------------------------------------------------------

    def _check_quotes_and_submit_straddle(self) -> None:
        """Validate option quotes freshness and submit short straddle SELL orders."""
        if not self.call_instrument_id or not self.put_instrument_id:
            return

        call_quote = self.market_data_manager.get_quote(self.call_instrument_id)
        put_quote = self.market_data_manager.get_quote(self.put_instrument_id)

        # In DRY_RUN mode with mock quotes or live quotes:
        if call_quote is None or put_quote is None:
            logger.debug("Waiting for both option quotes to arrive...")
            return

        # Validate quote freshness & spread sanity
        if not call_quote.is_valid or not put_quote.is_valid:
            logger.warning("Option quotes not yet valid or fresh; awaiting next tick.")
            return

        # Strict pricing rule: ask quote must be positive for buyback reference
        if call_quote.ask_price <= 0.0 or put_quote.ask_price <= 0.0:
            logger.warning("Invalid ask price (<=0) detected on options; aborting entry.")
            return

        logger.info(
            f"Option quotes validated:\n"
            f"  Call: Bid=${call_quote.bid_price:.2f}, Ask=${call_quote.ask_price:.2f} (Age={call_quote.quote_age_seconds:.2f}s)\n"
            f"  Put:  Bid=${put_quote.bid_price:.2f}, Ask=${put_quote.ask_price:.2f} (Age={put_quote.quote_age_seconds:.2f}s)"
        )

        self._execute_straddle_entry(call_quote, put_quote)

    def _execute_straddle_entry(
        self,
        call_quote: ValidatedQuote,
        put_quote: ValidatedQuote,
    ) -> None:
        """Construct and submit dual-leg SELL orders."""
        if self.orders_submitted or not self.order_manager or not self.selected_straddle:
            return

        self.orders_submitted = True
        self.strategy_state = StraddleStrategyState.SUBMITTING_ORDERS

        call_nautilus_inst = self.selected_straddle.call.nautilus_instrument
        put_nautilus_inst = self.selected_straddle.put.nautilus_instrument

        # For short straddle SELL limit orders, price at best bid (or mid)
        call_sell_px = call_quote.bid_price
        put_sell_px = put_quote.bid_price

        logger.info(
            f"Creating short straddle SELL orders (Quantity: {self.config.order_quantity} BTC each):\n"
            f"  Call SELL @ ${call_sell_px:.2f} (Bid)\n"
            f"  Put  SELL @ ${put_sell_px:.2f} (Bid)"
        )

        # Create straddle sell orders using OrderManager
        self.straddle_order = self.order_manager.create_straddle_sell_orders(
            call_instrument=call_nautilus_inst,
            put_instrument=put_nautilus_inst,
            quantity=self.config.order_quantity,
            call_price=call_sell_px,
            put_price=put_sell_px,
            unmatched_policy=UnmatchedLegPolicy(self.config.unmatched_leg_policy),
        )

        # Submit straddle
        self.strategy_state = StraddleStrategyState.AWAITING_FILLS
        self.entry_start_time_ns = time.time_ns()
        self.order_manager.submit_straddle(self.straddle_order)

        logger.info(
            f"Straddle orders submitted:\n"
            f"  Call CID: {self.straddle_order.call_leg.client_order_id} (Status: {self.straddle_order.call_leg.status.value})\n"
            f"  Put  CID: {self.straddle_order.put_leg.client_order_id} (Status: {self.straddle_order.put_leg.status.value})\n"
            f"  Composite Status: {self.straddle_order.status.value}"
        )

    # -------------------------------------------------------------------------
    # Execution & Order Event Callbacks
    # -------------------------------------------------------------------------

    def on_order_submitted(self, event: OrderSubmitted) -> None:
        """Handle OrderSubmitted event."""
        if self.order_manager:
            self.order_manager.on_order_submitted(event)

    def on_order_accepted(self, event: OrderAccepted) -> None:
        """Handle OrderAccepted event."""
        if self.order_manager:
            self.order_manager.on_order_accepted(event)

    def on_order_rejected(self, event: OrderRejected) -> None:
        """Handle OrderRejected event."""
        logger.warning(
            f"[on_order_rejected] Order rejected: cid={event.client_order_id}, reason='{event.reason}'"
        )
        if self.order_manager:
            self.order_manager.on_order_rejected(event)

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """Handle OrderCanceled event."""
        logger.info(f"[on_order_canceled] Order canceled: cid={event.client_order_id}")
        if self.order_manager:
            self.order_manager.on_order_canceled(event)

    def on_order_filled(self, event: OrderFilled) -> None:
        """
        Handle OrderFilled event.
        Updates OrderManager, applies fill to PositionTracker, and evaluates straddle state.
        """
        if not self.order_manager:
            return

        self.order_manager.on_order_filled(event)

        # Apply fill to PositionTracker
        inst_id = event.instrument_id
        side_str = "SELL" if event.order_side == OrderSide.SELL else "BUY"
        qty = float(event.last_qty)
        px = float(event.last_px)
        self.position_tracker.apply_fill(inst_id, side_str, qty, px)

        # Check straddle status
        if self.straddle_order:
            if self.straddle_order.is_one_leg_filled:
                logger.warning(
                    f"CRITICAL: Unmatched leg detected in strategy! "
                    f"Call={self.straddle_order.call_leg.status.value}, "
                    f"Put={self.straddle_order.put_leg.status.value}. "
                    f"Triggering policy {self.config.unmatched_leg_policy}..."
                )
                self.strategy_state = StraddleStrategyState.UNMATCHED_RECOVERING
                self.order_manager.resolve_unmatched_leg(self.straddle_order)

            elif self.straddle_order.is_fully_filled:
                self.strategy_state = StraddleStrategyState.POSITION_ACTIVE
                logger.info(
                    f"SUCCESS: Straddle position fully opened!\n"
                    f"  Call Leg: {self.straddle_order.call_leg.filled_quantity} BTC @ ${self.straddle_order.call_leg.average_price:.2f}\n"
                    f"  Put  Leg: {self.straddle_order.put_leg.filled_quantity} BTC @ ${self.straddle_order.put_leg.average_price:.2f}\n"
                    f"  Net Premium Collected: ${self.straddle_order.net_premium_collected:.2f} USDT"
                )

    # -------------------------------------------------------------------------
    # Position Callbacks
    # -------------------------------------------------------------------------

    def on_position_opened(self, event: PositionOpened) -> None:
        """Handle native NautilusTrader PositionOpened event."""
        self.position_tracker.on_position_event(event)

    def on_position_changed(self, event: PositionChanged) -> None:
        """Handle native NautilusTrader PositionChanged event."""
        self.position_tracker.on_position_event(event)

    def on_position_closed(self, event: PositionClosed) -> None:
        """Handle native NautilusTrader PositionClosed event."""
        self.position_tracker.on_position_event(event)

    # -------------------------------------------------------------------------
    # Private Helpers for NautilusTrader Live Mode
    # -------------------------------------------------------------------------

    def _submit_nautilus_order(self, order: Any) -> None:
        """Forward order submission to NautilusTrader native submit_order method."""
        try:
            self.submit_order(order)
        except Exception as e:
            logger.error(f"Error submitting order through NautilusTrader: {e}")
            raise

    def _cancel_nautilus_order(self, client_order_id: ClientOrderId) -> None:
        """Forward cancellation to NautilusTrader native cancel_order method."""
        try:
            # Look up order in Nautilus cache and cancel
            if hasattr(self, "cache") and self.cache:
                order = self.cache.order(client_order_id)
                if order:
                    self.cancel_order(order)
        except Exception as e:
            logger.error(f"Error canceling order {client_order_id}: {e}")
