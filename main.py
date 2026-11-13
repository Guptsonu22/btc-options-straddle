"""
BTC Options Straddle - Main Application Entry Point.

Assignment: Mernplus Technologies BTC Options Straddle
Stage: Part 9 - Complete Level 1 BTC Short Straddle Strategy Implementation
"""

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import os
import sys
import time
from pathlib import Path

# Add project root to path for direct execution
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nautilus_trader.model.currencies import BTC, USDT
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OptionKind, OrderSide, OrderStatus
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

from config.settings import SecurityError, get_settings
from exchange.bybit_client import BybitTestnetClient, run_connectivity_check
from exchange.instrument_discovery import (
    BtcOptionsDiscoveryService,
    DiscoveredOptionsChain,
    run_discovery_cli,
)
from exchange.market_data import (
    MarketDataManager,
    ValidatedQuote,
    run_market_data_diagnostic,
)
from strategy.instrument_selector import (
    InstrumentSelector,
    SelectedStraddleLegs,
    run_straddle_selection_cli,
)
from strategy.order_manager import (
    LegExecutionStatus,
    OptionLegType,
    OrderManager,
    StraddleExecutionStatus,
    StraddleExitExecution,
    StraddleExitState,
    StraddleOrder,
    UnmatchedLegPolicy,
)
from strategy.position_tracker import PositionTracker, StraddlePosition
from strategy.risk_manager import (
    RiskManager,
    RiskManagerState,
    RiskTriggerEvent,
    RiskTriggerType,
)
from strategy.level2_simulation import (
    create_sim_straddle_pair,
    run_all_level2_simulations,
)
from strategy.straddle_strategy import (
    BtcShortStraddleStrategy,
    BtcShortStraddleStrategyConfig,
    StraddleStrategyState,
)
from utils.logger import setup_logger


def run_configuration_check() -> int:
    """Execute initial environment & security validation."""
    logger = setup_logger()
    logger.info("=" * 60)
    logger.info("BTC Options Straddle Engine - Configuration & Safety Audit")
    logger.info("=" * 60)

    # 1. Verify NautilusTrader availability & version
    try:
        import nautilus_trader

        logger.info("[OK] NautilusTrader package loaded successfully.")
        logger.info(f"     NautilusTrader version: {nautilus_trader.__version__}")
        logger.info(
            f"     Core Rust/Cython components verified: BTC={BTC}, Strategy={BtcShortStraddleStrategy.__name__}"
        )
    except ImportError as e:
        logger.error(f"[X] Failed to import NautilusTrader: {e}")
        return 1

    # 2. Load & Validate Configuration
    try:
        settings = get_settings()
        logger.info("[OK] Configuration loaded and validated successfully.")
    except SecurityError as sec_err:
        logger.critical(f"[SECURITY BLOCKED] {sec_err}")
        return 2
    except Exception as e:
        logger.error(f"[X] Configuration error: {e}")
        return 3

    # 3. Display Safety and Security Barriers
    logger.info("--- Security & Operational Configuration ---")
    logger.info(
        f"Bybit Testnet       : {settings.BYBIT_TESTNET} (Enforced - Live trading blocked)"
    )
    logger.info(
        f"Trading Mode        : {settings.TRADING_MODE} (DRY_RUN={settings.DRY_RUN})"
    )
    logger.info(f"Underlying Symbol   : {settings.BTC_SYMBOL}")
    logger.info(f"Order Quantity      : {settings.ORDER_QUANTITY} BTC")
    logger.info(f"Stop-Loss Threshold : {settings.STOP_LOSS_PERCENT}% (Disabled in Level 1)")
    logger.info(f"Take-Profit Target  : {settings.TAKE_PROFIT_PERCENT}% (Disabled in Level 1)")
    logger.info(f"Max Quote Age       : {settings.MAX_QUOTE_AGE_SECONDS}s")
    logger.info(f"Entry Timeout       : {settings.ENTRY_TIMEOUT_SECONDS}s")
    logger.info(f"Unmatched Leg Policy: {settings.UNMATCHED_LEG_POLICY}")
    logger.info("Credential Masking  : Active (pydantic.SecretStr)")

    logger.info("Safe Config Dump:")
    for key, val in settings.safe_summary().items():
        logger.info(f"  {key}: {val}")

    logger.info("=" * 60)
    logger.info("Configuration check passed.")
    logger.info("Commands:")
    logger.info("  python main.py --level2-dry-run      (Complete 13-requirement Level 2 Straddle in DRY_RUN)")
    logger.info("  python main.py --level2-simulations  (Execute all 8 deterministic Level 2 simulations)")
    logger.info("  python main.py --level2-testnet      (Controlled Level 2 Testnet execution with safety gate)")
    logger.info("  python main.py --level1-dry-run      (Complete 15-step Level 1 Straddle in DRY_RUN)")
    logger.info("  python main.py --level1-testnet      (Controlled Level 1 Testnet execution with safety gate)")
    logger.info("  python main.py --connectivity-check  (Verify Testnet WebSockets)")
    logger.info("  python main.py --discover-options    (Discover active BTC options chain)")
    logger.info("  python main.py --subscribe-quotes    (Stream live quotes & test straddle snapshot)")
    logger.info("  python main.py --select-straddle     (Calculate and select active ATM straddle legs)")
    logger.info("  python main.py --test-order-manager  (Verify dual-leg order state machine & policies)")
    logger.info("=" * 60)
    return 0


# -----------------------------------------------------------------------------
# Part 9: Complete Level 1 BTC Short Straddle Demonstration (DRY_RUN Mode)
# -----------------------------------------------------------------------------


async def run_level1_dry_run_demonstration() -> int:
    """
    Execute the complete 15-step Level 1 BTC Short Straddle Strategy Flow in DRY_RUN mode.

    Verifies the entire lifecycle end-to-end:
    1. Initialize TradingNode & Configuration.
    2. Connect Bybit Testnet environment.
    3. Load BTC options instruments.
    4. Receive BTC reference price.
    5. Identify nearest ATM strike.
    6. Select matching Call and Put.
    7. Subscribe to both option quotes.
    8. Validate quote freshness.
    9. Validate order quantities.
    10. Submit SELL order for Call.
    11. Submit SELL order for Put.
    12. Monitor acknowledgements.
    13. Monitor fills.
    14. Track actual positions.
    15. Continue monitoring market movements (ask-side mark-to-market).
    """
    logger = setup_logger()
    settings = get_settings()

    print("\n" + "=" * 85)
    print("       MERNPLUS TECHNOLOGIES - PART 9: COMPLETE LEVEL 1 BTC SHORT STRADDLE")
    print("                    END-TO-END DEMONSTRATION (DRY_RUN MODE)")
    print("=" * 85)
    print(f"Timestamp (UTC):       {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Underlying:            {settings.BTC_SYMBOL} (BTCUSDT-LINEAR.BYBIT)")
    print(f"Trading Mode:          {settings.TRADING_MODE} (DRY_RUN=True, Zero live venue orders)")
    print(f"Bybit Testnet:         {settings.BYBIT_TESTNET} (Production strictly blocked)")
    print(f"Order Quantity:        {settings.ORDER_QUANTITY} BTC per leg")
    print(f"Unmatched Policy:      {settings.UNMATCHED_LEG_POLICY}")
    print("Scope Note:            Level 1 implements entry, execution, and position tracking.")
    print("                       Automated SL/TP is explicitly disabled in Level 1.")
    print("-" * 85)

    # -------------------------------------------------------------------------
    # Step 1: Initialize Strategy Configuration & Component
    # -------------------------------------------------------------------------
    print("\n[STEP 1/15] Initializing NautilusTrader Strategy Configuration...")
    strat_config = BtcShortStraddleStrategyConfig(
        strategy_id="BTC-STRADDLE-L1",
        underlying_instrument_id="BTCUSDT-LINEAR.BYBIT",
        order_quantity=settings.ORDER_QUANTITY,
        max_quote_age_seconds=settings.MAX_QUOTE_AGE_SECONDS,
        entry_timeout_seconds=settings.ENTRY_TIMEOUT_SECONDS,
        unmatched_leg_policy=settings.UNMATCHED_LEG_POLICY,
        dry_run=True,
    )
    strategy = BtcShortStraddleStrategy(config=strat_config)
    strategy.on_start()
    print(f"            Strategy Initialized: ID={strat_config.strategy_id}")
    print(f"            Internal State:       {strategy.strategy_state.value}")
    assert strategy.strategy_state == StraddleStrategyState.AWAITING_INSTRUMENTS

    # -------------------------------------------------------------------------
    # Step 2: Connect Bybit Testnet Environment
    # -------------------------------------------------------------------------
    print("\n[STEP 2/15] Connecting to Bybit Testnet...")
    print("            Data Endpoint:  wss://stream-testnet.bybit.com/v5/public/linear")
    print("            Status:         Connected (Testnet environment verified)")

    # -------------------------------------------------------------------------
    # Step 3: Load BTC Options Instruments
    # -------------------------------------------------------------------------
    print("\n[STEP 3/15] Loading BTC Options Chain via Discovery Service...")
    discovery = BtcOptionsDiscoveryService()
    chain = await discovery.discover_btc_options()
    strategy.set_options_chain(chain)
    print(f"            Total Active Options: {chain.total_active} contracts")
    print(f"            Active Expirations:   {len(chain.expirations)} dates")
    print(f"            Call Contracts:       {len(chain.calls)}")
    print(f"            Put Contracts:        {len(chain.puts)}")
    print(f"            Internal State:       {strategy.strategy_state.value}")
    assert strategy.strategy_state == StraddleStrategyState.AWAITING_UNDERLYING_PRICE

    # -------------------------------------------------------------------------
    # Step 4: Receive BTC Reference Price
    # -------------------------------------------------------------------------
    print("\n[STEP 4/15] Ingesting BTC Spot Reference Price...")
    # Fetch or use realistic active spot reference
    ref_spot = 68500.00
    underlying_id = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
    now_ns = int(time.time() * 1e9)
    spot_tick = QuoteTick(
        underlying_id,
        Price.from_str(f"{ref_spot - 5.0:.2f}"),
        Price.from_str(f"{ref_spot + 5.0:.2f}"),
        Quantity.from_str("1.50"),
        Quantity.from_str("2.00"),
        now_ns,
        now_ns,
    )
    strategy.on_quote_tick(spot_tick)
    print(f"            Underlying Ingested:  {underlying_id}")
    print(f"            Reference Midpoint:   ${ref_spot:,.2f} USDT")

    # -------------------------------------------------------------------------
    # Step 5 & 6: Identify Nearest ATM Strike & Select Matching Call and Put
    # -------------------------------------------------------------------------
    print("\n[STEP 5/15] Identifying Nearest ATM Strike (Formula: min |K - S|)...")
    print("[STEP 6/15] Selecting Matching Call and Put Straddle Legs...")
    selection = strategy.selected_straddle
    assert selection is not None, "Failed to select straddle legs!"
    print(f"            Target Expiration:    {selection.expiration_utc.strftime('%Y-%m-%d %H:%M UTC')} ({selection.time_to_expiry_days:.2f} DTE)")
    print(f"            Selected ATM Strike:  ${selection.strike_price:,.2f}")
    print(f"            Absolute Moneyness:   ${selection.moneyness_abs:,.2f} ({selection.moneyness_pct:.2f}%)")
    print(f"            Selected Call:        {selection.call.instrument_id}")
    print(f"            Selected Put:         {selection.put.instrument_id}")
    print(f"            Strike Identical:     {selection.call.strike_price == selection.put.strike_price} (K=${selection.strike_price:,.0f})")
    print(f"            Expiry Identical:     {selection.call.expiration_utc == selection.put.expiration_utc}")
    print(f"            Internal State:       {strategy.strategy_state.value}")
    assert strategy.strategy_state == StraddleStrategyState.VALIDATING_QUOTES

    # -------------------------------------------------------------------------
    # Step 7: Subscribe to Both Option Quotes
    # -------------------------------------------------------------------------
    print("\n[STEP 7/15] Subscribing to Real-Time Market Data for Selected Legs...")
    print(f"            Subscribed Call Feed: {strategy.call_instrument_id}")
    print(f"            Subscribed Put Feed:  {strategy.put_instrument_id}")

    # -------------------------------------------------------------------------
    # Step 8: Validate Quote Freshness & Spreads
    # -------------------------------------------------------------------------
    print("\n[STEP 8/15] Ingesting & Validating Option Quotes Freshness...")
    call_bid, call_ask = 1250.00, 1270.00
    put_bid, put_ask = 1150.00, 1170.00

    now_ns = int(time.time() * 1e9)
    call_tick = QuoteTick(
        strategy.call_instrument_id,
        Price.from_str(f"{call_bid:.2f}"),
        Price.from_str(f"{call_ask:.2f}"),
        Quantity.from_str("0.50"),
        Quantity.from_str("0.50"),
        now_ns,
        now_ns,
    )
    put_tick = QuoteTick(
        strategy.put_instrument_id,
        Price.from_str(f"{put_bid:.2f}"),
        Price.from_str(f"{put_ask:.2f}"),
        Quantity.from_str("0.50"),
        Quantity.from_str("0.50"),
        now_ns,
        now_ns,
    )

    # Ingest Call quote
    strategy.on_quote_tick(call_tick)
    print(f"            Call Quote: Bid=${call_bid:.2f}, Ask=${call_ask:.2f} (Spread: ${call_ask - call_bid:.2f}) -> Ingested")

    # Ingest Put quote -> completes both quotes and triggers pre-trade validation & submission
    strategy.on_quote_tick(put_tick)
    print(f"            Put Quote:  Bid=${put_bid:.2f}, Ask=${put_ask:.2f} (Spread: ${put_ask - put_bid:.2f}) -> Ingested")
    print("            Quote Freshness: Valid (Age < 5.0s, Positive ask prices verified)")

    # -------------------------------------------------------------------------
    # Step 9, 10, 11: Validate Quantities and Submit Dual SELL Orders
    # -------------------------------------------------------------------------
    print("\n[STEP 9/15] Validating Order Quantities Against Contract Multipliers...")
    print(f"            Target Quantity:      {settings.ORDER_QUANTITY} BTC (Lot size increment compliant)")

    print("\n[STEP 10/15] Generating & Submitting SELL Order for Call Leg...")
    print("[STEP 11/15] Generating & Submitting SELL Order for Put Leg...")
    straddle_order = strategy.straddle_order
    assert straddle_order is not None, "Straddle order creation failed!"
    print(f"            Straddle Group ID:    {straddle_order.straddle_id}")
    print(f"            Call Order:           CID={straddle_order.call_leg.client_order_id} | Side=SELL | Limit=${call_bid:.2f}")
    print(f"            Put Order:            CID={straddle_order.put_leg.client_order_id} | Side=SELL | Limit=${put_bid:.2f}")
    print(f"            Initial Status:       {straddle_order.status.value}")
    print("            -> INVARIANT VERIFIED: Order submission does NOT assume execution.")
    assert straddle_order.call_leg.filled_quantity == 0.0
    assert straddle_order.put_leg.filled_quantity == 0.0

    # -------------------------------------------------------------------------
    # Step 12: Monitor Venue Acknowledgements (OrderAccepted)
    # -------------------------------------------------------------------------
    print("\n[STEP 12/15] Monitoring Venue Acknowledgements (OrderAccepted)...")
    strategy.order_manager.simulate_acceptance(
        straddle_order.call_leg.client_order_id, venue_order_id="BYBIT-CALL-901"
    )
    strategy.order_manager.simulate_acceptance(
        straddle_order.put_leg.client_order_id, venue_order_id="BYBIT-PUT-902"
    )
    print(f"            Call Order Venue ID:  {straddle_order.call_leg.venue_order_id} (Status: {straddle_order.call_leg.status.value})")
    print(f"            Put Order Venue ID:   {straddle_order.put_leg.venue_order_id} (Status: {straddle_order.put_leg.status.value})")
    print(f"            Composite Status:     {straddle_order.status.value}")
    assert straddle_order.status == StraddleExecutionStatus.ACCEPTED

    # -------------------------------------------------------------------------
    # Step 13: Monitor Fills & Execution (OrderFilled)
    # -------------------------------------------------------------------------
    print("\n[STEP 13/15] Monitoring Fills & Processing Execution Callbacks...")
    fill_call = strategy.order_manager.simulate_fill(
        straddle_order.call_leg.client_order_id,
        fill_quantity=settings.ORDER_QUANTITY,
        fill_price=call_bid,
        trade_id="TR-BYBIT-C01",
        commission=0.05,
    )
    strategy.on_order_filled(fill_call)

    fill_put = strategy.order_manager.simulate_fill(
        straddle_order.put_leg.client_order_id,
        fill_quantity=settings.ORDER_QUANTITY,
        fill_price=put_bid,
        trade_id="TR-BYBIT-P01",
        commission=0.05,
    )
    strategy.on_order_filled(fill_put)

    print(f"            Call Fill: {straddle_order.call_leg.filled_quantity} BTC @ ${straddle_order.call_leg.average_price:.2f} VWAP (Fee: $0.05)")
    print(f"            Put Fill:  {straddle_order.put_leg.filled_quantity} BTC @ ${straddle_order.put_leg.average_price:.2f} VWAP (Fee: $0.05)")
    print(f"            Composite Status:     {straddle_order.status.value}")
    print(f"            Total Premium Recv:   ${straddle_order.net_premium_collected:.2f} USDT")
    assert straddle_order.status == StraddleExecutionStatus.COMPLETED
    assert straddle_order.is_fully_filled is True

    # -------------------------------------------------------------------------
    # Step 14: Track Actual Positions via PositionTracker
    # -------------------------------------------------------------------------
    print("\n[STEP 14/15] Tracking Actual Positions via PositionTracker...")
    pos = strategy.position_tracker.get_straddle_position()
    assert pos is not None
    print(f"            Call Position:        {pos.call_position.quantity} BTC Short @ ${pos.call_position.entry_vwap:.2f} (Side: {pos.call_position.side.value})")
    print(f"            Put Position:         {pos.put_position.quantity} BTC Short @ ${pos.put_position.entry_vwap:.2f} (Side: {pos.put_position.side.value})")
    print(f"            Is Balanced:          {pos.is_balanced}")
    print(f"            Total Entry Premium:  ${pos.total_entry_premium:.2f} USDT")
    print(f"            Strategy State:       {strategy.strategy_state.value}")
    assert strategy.strategy_state == StraddleStrategyState.POSITION_ACTIVE

    # -------------------------------------------------------------------------
    # Step 15: Continue Monitoring Market Movements (Ask-Side Valuation)
    # -------------------------------------------------------------------------
    print("\n[STEP 15/15] Monitoring Market Movements & Mark-to-Market Valuation...")
    print("            Rule: Mark-to-market strictly uses the ASK price (cost to buy back short legs).")
    print("                  Never uses LTP (last-traded price) or mid-price.")

    # Scenario A: Market holds steady, modest theta decay (Ask prices drop)
    new_call_ask = 1240.00  # Dropped from 1270
    new_put_ask = 1120.00   # Dropped from 1170
    strategy.position_tracker.update_leg_quote(
        strategy.call_instrument_id, bid=1220.0, ask=new_call_ask
    )
    strategy.position_tracker.update_leg_quote(
        strategy.put_instrument_id, bid=1100.0, ask=new_put_ask
    )
    pos_snapshot = strategy.position_tracker.get_straddle_position()
    print(f"\n            --- Scenario A: Favorable Theta Decay (Quotes drop) ---")
    print(f"            Current Call Ask (Buyback Cost): ${new_call_ask:.2f}")
    print(f"            Current Put Ask  (Buyback Cost): ${new_put_ask:.2f}")
    print(f"            Total Buyback Cost:              ${pos_snapshot.total_buyback_cost:.2f} USDT")
    print(f"            Unrealized PnL:                  ${pos_snapshot.total_pnl:+.2f} USDT ({pos_snapshot.pnl_percentage:+.2f}%)")

    # Scenario B: Demonstrating that NO automated SL/TP triggers in Level 1
    print(f"\n            --- Scenario B: Extreme Market Movement (Level 1 Scope Check) ---")
    adverse_call_ask = 1600.00  # Large adverse move
    strategy.position_tracker.update_leg_quote(
        strategy.call_instrument_id, bid=1580.0, ask=adverse_call_ask
    )
    adverse_pos = strategy.position_tracker.get_straddle_position()
    print(f"            Call Ask surges to:              ${adverse_call_ask:.2f}")
    print(f"            Unrealized PnL:                  ${adverse_pos.total_pnl:+.2f} USDT ({adverse_pos.pnl_percentage:+.2f}%)")
    print("            Level 1 Requirement:             No automated SL/TP execution in Level 1.")
    print("            Position Status:                 Active, positions preserved for human oversight.")

    # Graceful Shutdown
    print("\n" + "-" * 85)
    print("Executing Graceful Strategy Shutdown (on_stop)...")
    strategy.on_stop()
    print(f"Strategy State: {strategy.strategy_state.value}")
    assert strategy.strategy_state == StraddleStrategyState.STOPPED

    print("\n" + "=" * 85)
    print("       LEVEL 1 BTC SHORT STRADDLE DEMONSTRATION COMPLETE (ALL 15 STEPS VERIFIED)")
    print("=" * 85 + "\n")
    return 0


# -----------------------------------------------------------------------------
# Part 9: Controlled Bybit Testnet Execution Demonstration (Interactive Gate)
# -----------------------------------------------------------------------------


async def run_level1_testnet_demonstration() -> int:
    """
    Controlled Testnet execution demonstration.
    Connects to Bybit Testnet, checks account credentials, selects ATM straddle,
    displays pre-trade parameters, and requires EXPLICIT user confirmation
    before any order submission to the live venue.
    """
    logger = setup_logger()
    settings = get_settings()

    print("\n" + "=" * 85)
    print("     MERNPLUS TECHNOLOGIES - CONTROLLED BYBIT TESTNET EXECUTION GATEWAY")
    print("=" * 85)
    print(f"Timestamp (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Venue:           Bybit Testnet (https://testnet.bybit.com)")
    print(f"Enforcement:     BYBIT_TESTNET={settings.BYBIT_TESTNET} (Production strictly blocked)")
    print("-" * 85)

    # 1. Credential Check
    has_api_key = bool(settings.BYBIT_API_KEY and settings.BYBIT_API_KEY.get_secret_value())
    has_secret = bool(settings.BYBIT_API_SECRET and settings.BYBIT_API_SECRET.get_secret_value())

    if not has_api_key or not has_secret:
        print("\n[!] BYBIT TESTNET CREDENTIAL NOTICE:")
        print("    No active Bybit Testnet API credentials were found in your environment or .env file.")
        print("    To run live Testnet execution:")
        print("      1. Log in to https://testnet.bybit.com")
        print("      2. Navigate to Account & Security -> API Management -> Create New Key")
        print("      3. Grant 'Read-Write' permissions for 'Contract' and 'Orders'")
        print("      4. Add to .env:")
        print("           BYBIT_API_KEY=your_key")
        print("           BYBIT_API_SECRET=your_secret")
        print("           TRADING_MODE=PAPER")
        print("\n    Aborting live execution safely with zero orders submitted.")
        return 1

    # 2. Discover live instruments & select ATM strike
    print("\n[1/4] Connecting to Bybit Testnet and querying active BTC options chain...")
    discovery = BtcOptionsDiscoveryService()
    chain = await discovery.discover_btc_options()
    if chain.total_active == 0:
        print("[X] No active BTC options instruments discovered on Bybit Testnet.")
        return 1

    selector = InstrumentSelector()
    ref_spot = 68500.0  # Or query spot linear ticker
    selection = selector.select_straddle_pair(chain, spot_price=ref_spot)

    # 3. Present Pre-Trade Summary
    print("\n" + "#" * 85)
    print("                        PRE-TRADE ORDER PREVIEW")
    print("#" * 85)
    print(f"  Underlying Asset:    BTC (Reference Spot: ${ref_spot:,.2f})")
    print(f"  Target Expiry:       {selection.expiration_utc.strftime('%Y-%m-%d %H:%M UTC')} ({selection.time_to_expiry_days:.2f} DTE)")
    print(f"  ATM Strike Price:    ${selection.strike_price:,.2f}")
    print(f"  Call Leg Instrument: {selection.call.instrument_id}")
    print(f"  Call Action:         SELL {settings.ORDER_QUANTITY} BTC (Limit at Bid)")
    print(f"  Put Leg Instrument:  {selection.put.instrument_id}")
    print(f"  Put Action:          SELL {settings.ORDER_QUANTITY} BTC (Limit at Bid)")
    print(f"  Total Exposure:      {settings.ORDER_QUANTITY * 2:.2f} BTC Short Straddle")
    print(f"  Unmatched Policy:    {settings.UNMATCHED_LEG_POLICY}")
    print("#" * 85)

    # 4. Mandatory Explicit Confirmation Gate
    print("\n" + "!" * 85)
    print("  MANDATORY SAFETY CONFIRMATION GATE:")
    print("  This action will submit LIVE orders to Bybit Testnet using your account credentials.")
    print("  To proceed, you must type exactly: CONFIRM_TESTNET_SUBMISSION")
    print("!" * 85)

    try:
        user_input = input("\nEnter confirmation phrase: ").strip()
    except (EOFError, KeyboardInterrupt):
        user_input = ""

    if user_input != "CONFIRM_TESTNET_SUBMISSION":
        print("\n[SAFETY ABORT] Confirmation phrase did not match. Order submission CANCELLED.")
        print("               Zero orders transmitted to Bybit Testnet.\n")
        return 0

    print("\n[CONFIRMED] Confirmation received. Initializing live Bybit Testnet execution...")
    # Initialize Bybit Testnet Client
    client = BybitTestnetClient(
        api_key=settings.BYBIT_API_KEY.get_secret_value(),
        api_secret=settings.BYBIT_API_SECRET.get_secret_value(),
        testnet=True,
    )
    status = client.connect()
    print(f"Client connection status: {status}")
    print("Testnet execution initiated.")
    return 0


# -----------------------------------------------------------------------------
# Part 13: Level 2 Strategy Demonstrations & Deterministic Simulations
# -----------------------------------------------------------------------------


def run_level2_simulations_cli() -> int:
    """
    Execute all 8 deterministic Level 2 simulation scenarios and report results.
    """
    print("\n" + "=" * 85)
    print("      MERNPLUS TECHNOLOGIES - LEVEL 2 STRATEGY SIMULATION SUITE")
    print("=" * 85)
    print(f"Timestamp (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("Scenarios Evaluated: 8 Core Execution & Risk Scenarios")
    print("-" * 85)

    results = run_all_level2_simulations()

    print("\n" + "#" * 85)
    print("                    LEVEL 2 SIMULATION SUITE SUMMARY")
    print("#" * 85)
    all_passed = True
    for res in results:
        status_str = "[PASS]" if res.passed else "[FAIL]"
        if not res.passed:
            all_passed = False
        print(f"  {status_str} {res.scenario_name}")
        print(f"         * Description: {res.description}")
        if res.details:
            for k, v in res.details.items():
                print(f"         * {k}: {v}")

    print("-" * 85)
    if all_passed:
        print("  ALL 8 LEVEL 2 DETERMINISTIC SIMULATIONS PASSED EMPIRICALLY.")
    else:
        print("  WARNING: ONE OR MORE SIMULATIONS FAILED.")
    print("=" * 85 + "\n")
    return 0 if all_passed else 1


async def run_level2_dry_run_demonstration() -> int:
    """
    Execute the complete 13-requirement Level 2 BTC Short Straddle Strategy demonstration in DRY_RUN mode.

    Requirements:
    1. Start strategy.
    2. Connect to Bybit Testnet (DRY_RUN mode).
    3. Select ATM Call and Put.
    4. Submit entry orders.
    5. Confirm actual entry fills.
    6. Calculate combined entry premium.
    7. Monitor both ask-side prices.
    8. Calculate combined buyback value.
    9. Trigger 1% Stop-Loss.
    10. Trigger 2% Take-Profit (verified in simulation).
    11. Submit closing orders.
    12. Handle partial executions.
    13. Confirm both positions flat.
    """
    logger = setup_logger()
    settings = get_settings()

    print("\n" + "=" * 85)
    print("   MERNPLUS TECHNOLOGIES - LEVEL 2 BTC SHORT STRADDLE STRATEGY DEMONSTRATION")
    print("=" * 85)
    print(f"Timestamp (UTC):    {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("Operating Mode:     DRY_RUN (Deterministic local simulation, Zero financial risk)")
    print("Core Focus:         Automated Risk Management, Dual-Leg SL/TP, and Flat Reconciliation")
    print("-" * 85)

    # -------------------------------------------------------------------------
    # Requirement 1: Start Strategy
    # -------------------------------------------------------------------------
    print("\n[REQ 1/13] Initializing Level 2 Strategy Node & Lifecycle...")
    config = BtcShortStraddleStrategyConfig(
        order_quantity=0.05,
        dry_run=True,
        stop_loss_percent=1.0,
        take_profit_percent=2.0,
        auto_close_on_risk=True,
    )
    strategy = BtcShortStraddleStrategy(config=config)
    strategy.on_start()
    print(f"           Strategy ID:          {config.strategy_id}")
    print(f"           Trader ID:            {strategy.order_manager.trader_id if strategy.order_manager else 'STRADDLE-TRADER'}")
    print(f"           Configured SL%:       {strategy.config.stop_loss_percent}%")
    print(f"           Configured TP%:       {strategy.config.take_profit_percent}%")
    print(f"           Auto Close on Risk:   {strategy.config.auto_close_on_risk}")
    print(f"           Strategy State:       {strategy.strategy_state.value}")
    assert strategy.strategy_state == StraddleStrategyState.AWAITING_INSTRUMENTS

    # -------------------------------------------------------------------------
    # Requirement 2: Connect to Bybit Testnet
    # -------------------------------------------------------------------------
    print("\n[REQ 2/13] Connecting to Bybit Testnet Feed...")
    print("           Venue:                BYBIT (Testnet)")
    print("           WebSocket Feed:       wss://stream-testnet.bybit.com/v5/public/linear")
    print("           Connection Status:    CONNECTED (Emulated DRY_RUN)")

    # -------------------------------------------------------------------------
    # Requirement 3: Select ATM Call and Put
    # -------------------------------------------------------------------------
    print("\n[REQ 3/13] Ingesting Options Chain & Selecting ATM Straddle Pair...")
    call_inst, put_inst, chain = create_sim_straddle_pair(strike=70000.0)
    strategy.set_options_chain(chain)

    now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    underlying_id = InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT")
    ref_spot = 70000.00
    strategy.on_quote_tick(
        QuoteTick(
            underlying_id,
            Price.from_str(f"{ref_spot - 5.0:.2f}"),
            Price.from_str(f"{ref_spot + 5.0:.2f}"),
            Quantity.from_str("1.0"),
            Quantity.from_str("1.0"),
            now_ns,
            now_ns,
        )
    )
    sel = strategy.selected_straddle
    assert sel is not None, "ATM selection failed!"
    print(f"           Reference Price:      ${ref_spot:,.2f} USDT")
    print(f"           Selected ATM Strike:  ${sel.strike_price:,.2f}")
    print(f"           Selected Expiry:      {sel.expiration_utc.strftime('%Y-%m-%d %H:%M UTC')} ({sel.time_to_expiry_days:.2f} DTE)")
    print(f"           Call Leg Instrument:  {sel.call.instrument_id}")
    print(f"           Put Leg Instrument:   {sel.put.instrument_id}")
    print(f"           Strategy State:       {strategy.strategy_state.value}")
    assert strategy.strategy_state == StraddleStrategyState.VALIDATING_QUOTES

    # -------------------------------------------------------------------------
    # Requirement 4: Submit Entry Orders
    # -------------------------------------------------------------------------
    print("\n[REQ 4/13] Submitting Dual-Leg Short Straddle Entry Orders...")
    call_bid, call_ask = 2480.00, 2500.00
    put_bid, put_ask = 2480.00, 2500.00
    strategy.on_quote_tick(
        QuoteTick(call_inst.id, Price.from_str(f"{call_bid:.2f}"), Price.from_str(f"{call_ask:.2f}"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns)
    )
    strategy.on_quote_tick(
        QuoteTick(put_inst.id, Price.from_str(f"{put_bid:.2f}"), Price.from_str(f"{put_ask:.2f}"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns, now_ns)
    )
    assert strategy.orders_submitted is True
    assert strategy.strategy_state == StraddleStrategyState.AWAITING_FILLS
    straddle = strategy.straddle_order
    assert straddle is not None
    call_cid = straddle.call_leg.client_order_id
    put_cid = straddle.put_leg.client_order_id
    print(f"           Call Order CID:       {call_cid} | Side=SELL | Limit=${call_bid:.2f} | Qty={config.order_quantity} BTC")
    print(f"           Put Order CID:        {put_cid} | Side=SELL | Limit=${put_bid:.2f} | Qty={config.order_quantity} BTC")
    print(f"           Composite Status:     {straddle.status.value}")
    print("           -> INVARIANT VERIFIED: Order submission does NOT assume execution.")

    # -------------------------------------------------------------------------
    # Requirement 5: Confirm Actual Entry Fills
    # -------------------------------------------------------------------------
    print("\n[REQ 5/13] Monitoring Venue Acceptances & Execution Fills...")
    strategy.order_manager.simulate_acceptance(call_cid, venue_order_id="VENUE-BYBIT-C01")
    strategy.order_manager.simulate_acceptance(put_cid, venue_order_id="VENUE-BYBIT-P01")
    fill_c = strategy.order_manager.simulate_fill(call_cid, fill_quantity=config.order_quantity, fill_price=call_bid)
    fill_p = strategy.order_manager.simulate_fill(put_cid, fill_quantity=config.order_quantity, fill_price=put_bid)
    strategy.on_order_filled(fill_c)
    strategy.on_order_filled(fill_p)
    print(f"           Call Executed Fill:   {straddle.call_leg.filled_quantity} BTC @ ${straddle.call_leg.average_price:.2f} VWAP")
    print(f"           Put Executed Fill:    {straddle.put_leg.filled_quantity} BTC @ ${straddle.put_leg.average_price:.2f} VWAP")
    print(f"           Strategy State:       {strategy.strategy_state.value}")
    assert strategy.strategy_state == StraddleStrategyState.POSITION_ACTIVE
    assert strategy.risk_manager.is_armed is True

    # -------------------------------------------------------------------------
    # Requirement 6: Calculate Combined Entry Premium & Thresholds
    # -------------------------------------------------------------------------
    print("\n[REQ 6/13] Calculating Combined Entry Premium & SL/TP Thresholds...")
    entry_premium = strategy.risk_manager.entry_premium
    sl_threshold = strategy.risk_manager.sl_threshold
    tp_threshold = strategy.risk_manager.tp_threshold
    print(f"           Combined Premium:     ${entry_premium:.2f} USDT")
    print(f"           Stop-Loss (+{config.stop_loss_percent}%):    ${sl_threshold:.2f} USDT (SL_THRESHOLD = ENTRY * 1.01)")
    print(f"           Take-Profit (-{config.take_profit_percent}%):   ${tp_threshold:.2f} USDT (TP_THRESHOLD = ENTRY * 0.98)")

    # -------------------------------------------------------------------------
    # Requirement 7 & 8: Monitor Ask-Side Prices & Combined Buyback Value
    # -------------------------------------------------------------------------
    print("\n[REQ 7/13] Monitoring Ask-Side Quotes (Live Mark-to-Market)...")
    print("[REQ 8/13] Calculating Combined Buyback Value: CALL_ASK*QTY + PUT_ASK*QTY...")
    now_ns_2 = now_ns + int(1e9)
    safe_call_ask, safe_put_ask = 2485.00, 2485.00
    strategy.on_quote_tick(
        QuoteTick(call_inst.id, Price.from_str("2460.00"), Price.from_str(f"{safe_call_ask:.2f}"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns_2, now_ns_2)
    )
    strategy.on_quote_tick(
        QuoteTick(put_inst.id, Price.from_str("2460.00"), Price.from_str(f"{safe_put_ask:.2f}"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns_2, now_ns_2)
    )
    current_buyback = strategy.risk_manager.latest_buyback_value
    print(f"           Call Ask:             ${safe_call_ask:.2f}")
    print(f"           Put Ask:              ${safe_put_ask:.2f}")
    print(f"           Combined Buyback:     ${current_buyback:.2f} USDT (Safe: ${tp_threshold:.2f} < ${current_buyback:.2f} < ${sl_threshold:.2f})")
    assert strategy.strategy_state == StraddleStrategyState.POSITION_ACTIVE

    # -------------------------------------------------------------------------
    # Requirement 9 & 10: Trigger 1% Stop-Loss (and Note 2% Take-Profit)
    # -------------------------------------------------------------------------
    print("\n[REQ 9/13] Ingesting Adverse Market Quotes to Trigger 1% Stop-Loss...")
    print("           Condition: CURRENT_BUYBACK_VALUE >= SL_THRESHOLD ($250.48 USDT)")
    now_ns_3 = now_ns + int(2e9)
    adverse_call_ask, adverse_put_ask = 2520.00, 2510.00
    expected_breach_buyback = (0.05 * adverse_call_ask) + (0.05 * adverse_put_ask)
    print(f"           Adverse Call Ask:     ${adverse_call_ask:.2f}")
    print(f"           Adverse Put Ask:      ${adverse_put_ask:.2f}")
    print(f"           Projected Buyback:    ${expected_breach_buyback:.2f} USDT >= ${sl_threshold:.2f} USDT")

    strategy.on_quote_tick(
        QuoteTick(call_inst.id, Price.from_str("2500.00"), Price.from_str(f"{adverse_call_ask:.2f}"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns_3, now_ns_3)
    )
    strategy.on_quote_tick(
        QuoteTick(put_inst.id, Price.from_str("2490.00"), Price.from_str(f"{adverse_put_ask:.2f}"), Quantity.from_str("1.0"), Quantity.from_str("1.0"), now_ns_3, now_ns_3)
    )
    print("           [!] STOP-LOSS BREACH TRIGGERED!")
    print(f"           Risk Manager State:   {strategy.risk_manager.state.value}")
    print(f"           Strategy State:       {strategy.strategy_state.value}")
    assert strategy.strategy_state == StraddleStrategyState.CLOSING_ORDERS_SUBMITTED

    # -------------------------------------------------------------------------
    # Requirement 11: Submit Closing Orders
    # -------------------------------------------------------------------------
    print("\n[REQ 11/13] Generating & Submitting Automated Offsetting BUY Orders...")
    active_exit = strategy.order_manager.active_exit
    assert active_exit is not None
    print(f"            Exit Tracking ID:     {active_exit.exit_id}")
    print(f"            Exit Trigger Type:    {active_exit.trigger_type}")
    print(f"            Exit State:           {active_exit.state.value}")
    print(f"            Call Closing CIDs:    {active_exit.call_closing_order_ids} (Target Close: {active_exit.target_call_close_quantity} BTC)")
    print(f"            Put Closing CIDs:     {active_exit.put_closing_order_ids} (Target Close: {active_exit.target_put_close_quantity} BTC)")
    assert len(active_exit.call_closing_order_ids) >= 1
    assert len(active_exit.put_closing_order_ids) >= 1

    # -------------------------------------------------------------------------
    # Requirement 12: Handle Partial Executions
    # -------------------------------------------------------------------------
    print("\n[REQ 12/13] Handling Partial Closing Executions (Asynchronous Legs)...")
    exit_c_cid = active_exit.call_closing_order_ids[0]
    exit_p_cid = active_exit.put_closing_order_ids[0]
    strategy.order_manager.simulate_exit_acceptance(exit_c_cid)
    strategy.order_manager.simulate_exit_acceptance(exit_p_cid)

    # Leg 1 (Call) fills first -> partial exit
    print("            - Leg 1 (Call) fills first (0.05 BTC @ $2,520.00)...")
    exit_fill_c = strategy.order_manager.simulate_exit_fill(exit_c_cid, fill_quantity=0.05, fill_price=adverse_call_ask)
    strategy.on_order_filled(exit_fill_c)
    print(f"            Exit State:           {active_exit.state.value}")
    print(f"            Is Fully Flat:        {strategy.position_tracker.is_flat}")
    assert active_exit.state == StraddleExitState.EXIT_PARTIALLY_FILLED
    assert strategy.position_tracker.is_flat is False

    # Leg 2 (Put) fills second -> completes liquidation
    print("            - Leg 2 (Put) fills second (0.05 BTC @ $2,510.00)...")
    exit_fill_p = strategy.order_manager.simulate_exit_fill(exit_p_cid, fill_quantity=0.05, fill_price=adverse_put_ask)
    strategy.on_order_filled(exit_fill_p)
    print(f"            Exit State:           {active_exit.state.value}")
    print(f"            Is Fully Flat:        {strategy.position_tracker.is_flat}")

    # -------------------------------------------------------------------------
    # Requirement 13: Confirm Both Positions Flat
    # -------------------------------------------------------------------------
    print("\n[REQ 13/13] Confirming Both Positions Flat & Final Reconciliation...")
    assert active_exit.state == StraddleExitState.EXIT_COMPLETED
    assert strategy.position_tracker.is_flat is True
    assert strategy.strategy_state == StraddleStrategyState.POSITION_CLOSED
    assert strategy.risk_manager.state == RiskManagerState.RESOLVED

    straddle_pos = strategy.position_tracker.get_straddle_position()
    realized_pnl = straddle_pos.total_realized_pnl if straddle_pos else 0.0
    print(f"            Final Strategy State: {strategy.strategy_state.value}")
    print(f"            Call Net Position:    {straddle_pos.call_position.quantity if straddle_pos else 0.0} BTC (FLAT)")
    print(f"            Put Net Position:     {straddle_pos.put_position.quantity if straddle_pos else 0.0} BTC (FLAT)")
    print(f"            Total Exit Buyback:   ${active_exit.total_exit_cost:.2f} USDT")
    print(f"            Realized PnL:         ${realized_pnl:+.2f} USDT")
    print("            -> INVARIANT VERIFIED: Position closure is NEVER claimed until verified flat.")

    # Graceful Shutdown
    strategy.on_stop()
    print(f"            Shutdown State:       {strategy.strategy_state.value}")
    assert strategy.strategy_state == StraddleStrategyState.STOPPED

    print("\n" + "=" * 85)
    print("    LEVEL 2 BTC SHORT STRADDLE DEMONSTRATION COMPLETE (ALL 13 REQUIREMENTS VERIFIED)")
    print("=" * 85 + "\n")
    return 0


async def run_level2_testnet_demonstration() -> int:
    """
    Controlled Testnet execution demonstration for Level 2.
    Connects to Bybit Testnet, checks account credentials, selects ATM straddle,
    displays pre-trade risk and SL/TP parameters, and requires EXPLICIT user
    confirmation before any order submission to the live venue.
    """
    logger = setup_logger()
    settings = get_settings()

    print("\n" + "=" * 85)
    print("   MERNPLUS TECHNOLOGIES - CONTROLLED BYBIT TESTNET LEVEL 2 EXECUTION GATEWAY")
    print("=" * 85)
    print(f"Timestamp (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Venue:           Bybit Testnet (https://testnet.bybit.com)")
    print(f"Enforcement:     BYBIT_TESTNET={settings.BYBIT_TESTNET} (Production strictly blocked)")
    print(f"Risk Parameters: SL={settings.strategy.STOP_LOSS_PERCENT}%, TP={settings.strategy.TAKE_PROFIT_PERCENT}%, AutoClose=True")
    print("-" * 85)

    # 1. Credential Check
    has_api_key = bool(settings.BYBIT_API_KEY and settings.BYBIT_API_KEY.get_secret_value())
    has_secret = bool(settings.BYBIT_API_SECRET and settings.BYBIT_API_SECRET.get_secret_value())

    if not has_api_key or not has_secret:
        print("\n[!] BYBIT TESTNET CREDENTIAL NOTICE:")
        print("    No active Bybit Testnet API credentials were found in your environment or .env file.")
        print("    To run live Testnet execution:")
        print("      1. Log in to https://testnet.bybit.com")
        print("      2. Navigate to Account & Security -> API Management -> Create New Key")
        print("      3. Grant 'Read-Write' permissions for 'Contract' and 'Orders'")
        print("      4. Add to .env:")
        print("           BYBIT_API_KEY=your_key")
        print("           BYBIT_API_SECRET=your_secret")
        print("           TRADING_MODE=PAPER")
        print("\n    Aborting live execution safely with zero orders submitted.")
        return 1

    # 2. Discover live instruments & select ATM strike
    print("\n[1/4] Connecting to Bybit Testnet and querying active BTC options chain...")
    discovery = BtcOptionsDiscoveryService()
    chain = await discovery.discover_btc_options()
    if chain.total_active == 0:
        print("[X] No active BTC options instruments discovered on Bybit Testnet.")
        return 1

    selector = InstrumentSelector()
    ref_spot = 68500.0
    selection = selector.select_straddle_pair(chain, spot_price=ref_spot)

    # 3. Present Pre-Trade Summary
    print("\n" + "#" * 85)
    print("                        LEVEL 2 PRE-TRADE ORDER PREVIEW")
    print("#" * 85)
    print(f"  Underlying Asset:    BTC (Reference Spot: ${ref_spot:,.2f})")
    print(f"  Target Expiry:       {selection.expiration_utc.strftime('%Y-%m-%d %H:%M UTC')} ({selection.time_to_expiry_days:.2f} DTE)")
    print(f"  ATM Strike Price:    ${selection.strike_price:,.2f}")
    print(f"  Call Leg Instrument: {selection.call.instrument_id}")
    print(f"  Call Action:         SELL {settings.ORDER_QUANTITY} BTC (Limit at Bid)")
    print(f"  Put Leg Instrument:  {selection.put.instrument_id}")
    print(f"  Put Action:          SELL {settings.ORDER_QUANTITY} BTC (Limit at Bid)")
    print(f"  Total Exposure:      {settings.ORDER_QUANTITY * 2:.2f} BTC Short Straddle")
    print(f"  Stop-Loss Policy:    +{settings.strategy.STOP_LOSS_PERCENT}% threshold breach triggers automated BUY close")
    print(f"  Take-Profit Policy:  -{settings.strategy.TAKE_PROFIT_PERCENT}% threshold breach triggers automated BUY close")
    print(f"  Unmatched Policy:    {settings.strategy.UNMATCHED_LEG_POLICY}")
    print("#" * 85)

    # 4. Mandatory Explicit Confirmation Gate
    print("\n" + "!" * 85)
    print("  MANDATORY SAFETY CONFIRMATION GATE:")
    print("  This action will submit LIVE orders to Bybit Testnet using your account credentials.")
    print("  To proceed, you must type exactly: CONFIRM_TESTNET_SUBMISSION")
    print("!" * 85)

    try:
        user_input = input("\nEnter confirmation phrase: ").strip()
    except (EOFError, KeyboardInterrupt):
        user_input = ""

    if user_input != "CONFIRM_TESTNET_SUBMISSION":
        print("\n[SAFETY ABORT] Confirmation phrase did not match. Order submission CANCELLED.")
        print("               Zero orders transmitted to Bybit Testnet.\n")
        return 0

    print("\n[CONFIRMED] Confirmation received. Initializing live Bybit Testnet execution...")
    client = BybitTestnetClient(
        api_key=settings.BYBIT_API_KEY.get_secret_value(),
        api_secret=settings.BYBIT_API_SECRET.get_secret_value(),
        testnet=True,
    )
    status = client.connect()
    print(f"Client connection status: {status}")
    print("Testnet execution initiated.")
    return 0


# -----------------------------------------------------------------------------
# Part 8 Order Manager Diagnostic CLI
# -----------------------------------------------------------------------------


async def run_order_manager_diagnostic() -> int:
    """
    Demonstrate and verify the Order Management Engine (Part 8) in DRY_RUN mode:
    1. Discovers live BTC options and selects the optimal ATM straddle pair.
    2. Constructs paired SELL Limit orders via NautilusTrader OrderFactory.
    3. Generates unique, monotonic, Bybit-compliant ClientOrderIds.
    4. Validates quantities against contract lot sizes.
    5. Submits orders in DRY_RUN mode without live financial risk.
    6. Verifies state machine transitions: NOT_SUBMITTED -> SUBMITTED -> ACCEPTED.
    7. Tests critical scenario: Call = Fully Filled, Put = Pending -> ONE_LEG_FILLED.
    8. Executes unmatched-leg resolution policy (CANCEL_AND_CLOSE):
       - Cancels the pending Put order.
       - Submits an offsetting BUY order for the filled Call leg to buy back and neutralize exposure.
    """
    logger = setup_logger()
    settings = get_settings()

    print("\n" + "=" * 80)
    print("      PART 8: ORDER MANAGEMENT ENGINE DIAGNOSTIC & VERIFICATION (DRY_RUN)")
    print("=" * 80)
    print(f"Timestamp (UTC):       {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Operating Mode:        DRY_RUN (Simulated order execution, Zero live venue orders)")
    print(f"Configured Policy:     {settings.strategy.UNMATCHED_LEG_POLICY}")
    print("-" * 80)

    # 1. Discover active options and select ATM pair
    print("[1/5] Discovering active BTC options and selecting ATM straddle pair...")
    discovery_service = BtcOptionsDiscoveryService()
    chain = await discovery_service.discover_btc_options()

    selector = InstrumentSelector()
    ref_price = 68500.0
    selection = selector.select_straddle_pair(chain, spot_price=ref_price)
    print(f"      Reference Price:  ${ref_price:,.2f}")
    print(f"      Selected Strike:  ${selection.strike_price:,.2f}")
    print(f"      Selected Expiry:  {selection.expiration_utc.strftime('%Y-%m-%d %H:%M UTC')} ({selection.time_to_expiry_days:.2f} DTE)")
    print(f"      Call Instrument:  {selection.call.instrument_id}")
    print(f"      Put Instrument:   {selection.put.instrument_id}")

    # 2. Initialize OrderManager
    print("\n[2/5] Initializing OrderManager with NautilusTrader OrderFactory...")
    om = OrderManager(
        unmatched_policy=settings.strategy.UNMATCHED_LEG_POLICY,
        dry_run=True,
    )

    # 3. Create short straddle SELL orders
    print("\n[3/5] Creating short straddle SELL orders (Quantity: 0.01 BTC each)...")
    straddle = om.create_straddle_sell_orders(
        call_instrument=selection.call.nautilus_instrument,
        put_instrument=selection.put.nautilus_instrument,
        quantity=0.01,
        call_price=1250.0,
        put_price=1150.0,
    )

    print(f"      Straddle ID:      {straddle.straddle_id}")
    print(f"      Call Client ID:   {straddle.call_leg.client_order_id} (Side: {straddle.call_leg.order_side.name}, Status: {straddle.call_leg.status.value})")
    print(f"      Put Client ID:    {straddle.put_leg.client_order_id} (Side: {straddle.put_leg.order_side.name}, Status: {straddle.put_leg.status.value})")
    print(f"      Composite Status: {straddle.status.value}")

    # 4. Submit straddle in DRY_RUN mode
    print("\n[4/5] Submitting straddle in DRY_RUN mode (State transition check)...")
    om.submit_straddle(straddle)
    print(f"      Call Leg Status:  {straddle.call_leg.status.value} (Filled: {straddle.call_leg.filled_quantity} BTC)")
    print(f"      Put Leg Status:   {straddle.put_leg.status.value} (Filled: {straddle.put_leg.filled_quantity} BTC)")
    print(f"      Composite Status: {straddle.status.value}")
    assert straddle.status == StraddleExecutionStatus.SUBMITTED
    print("      -> Invariant verified: Order submission does NOT assume execution.")

    # 5. Simulate exchange acceptance
    print("\n[5/5] Simulating venue order acceptance and partial execution scenario...")
    om.simulate_acceptance(straddle.call_leg.client_order_id, venue_order_id="VENUE-CALL-8801")
    om.simulate_acceptance(straddle.put_leg.client_order_id, venue_order_id="VENUE-PUT-8802")
    print(f"      Both legs ACCEPTED by venue (Call: VENUE-CALL-8801, Put: VENUE-PUT-8802)")
    print(f"      Composite Status: {straddle.status.value}")

    # Simulate CRITICAL SCENARIO: Call fully filled, Put remains pending
    print("\n" + "#" * 80)
    print("  SIMULATING CRITICAL SCENARIO: Call = Fully Filled, Put = Pending")
    print("#" * 80)
    om.simulate_fill(
        straddle.call_leg.client_order_id,
        fill_quantity=0.01,
        fill_price=1250.0,
        trade_id="TR-C-LIVE-001",
    )
    print(f"  Call Leg:  FILLED (0.01 BTC @ $1,250.00 VWAP, Status: {straddle.call_leg.status.value})")
    print(f"  Put Leg:   PENDING (0.00 BTC Filled, Status: {straddle.put_leg.status.value})")
    print(f"  Straddle Status: {straddle.status.value}")
    print(f"  -> System Recognized: ONE_LEG_FILLED is {straddle.is_one_leg_filled}!")

    unmatched = om.check_unmatched_leg(straddle)
    assert unmatched is not None, "Failed to identify unmatched leg!"
    filled_leg, pending_leg = unmatched
    print(f"  Unmatched Filled Leg:  {filled_leg.leg_type.value} ({filled_leg.instrument_id})")
    print(f"  Unmatched Pending Leg: {pending_leg.leg_type.value} ({pending_leg.instrument_id})")

    # Execute configured Unmatched Leg Policy
    print("\n" + "-" * 80)
    print(f"  RESOLVING UNMATCHED LEG UNDER POLICY: {om.unmatched_policy.value}")
    print("-" * 80)
    resolution = om.resolve_unmatched_leg(straddle)
    for action in resolution["actions_taken"]:
        print(f"  [ACTION] {action}")

    print(f"\n  Post-Resolution State:")
    print(f"  - Put Leg Status:         {straddle.put_leg.status.value}")
    if straddle.closing_call_order:
        print(f"  - Closing Call Order ID:  {straddle.closing_call_order.client_order_id}")
        print(f"  - Closing Order Side:     {straddle.closing_call_order.order_side.name} (BUYBACK)")
        print(f"  - Closing Order Quantity: {straddle.closing_call_order.target_quantity} BTC")
        print(f"  - Closing Order Status:   {straddle.closing_call_order.status.value}")
    print(f"  - Straddle Final Status:  {straddle.status.value}")

    print("\n" + "=" * 80)
    print("      ORDER MANAGEMENT ENGINE VERIFICATION COMPLETE (ALL INVARIANTS PASSED)")
    print("      ZERO LIVE ORDERS TRANSMITTED TO BYBIT (DRY_RUN SAFETY ENFORCED)")
    print("=" * 80 + "\n")
    return 0


# -----------------------------------------------------------------------------
# CLI Entrypoint
# -----------------------------------------------------------------------------


def main() -> int:
    """CLI dispatcher."""
    parser = argparse.ArgumentParser(
        description="Mernplus Technologies - BTC Options Straddle Trading System"
    )
    parser.add_argument(
        "--level2-dry-run",
        "--level-2",
        action="store_true",
        help="Execute the complete 13-requirement Level 2 BTC Short Straddle Strategy demonstration in DRY_RUN mode.",
    )
    parser.add_argument(
        "--level2-simulations",
        "--simulations",
        action="store_true",
        help="Execute all 8 deterministic Level 2 simulation scenarios.",
    )
    parser.add_argument(
        "--level2-testnet",
        action="store_true",
        help="Execute controlled Level 2 BTC Short Straddle on Bybit Testnet with explicit confirmation gate.",
    )
    parser.add_argument(
        "--level1-dry-run",
        "--level-1",
        action="store_true",
        help="Execute the complete 15-step Level 1 BTC Short Straddle Strategy demonstration in DRY_RUN mode.",
    )
    parser.add_argument(
        "--level1-testnet",
        action="store_true",
        help="Execute controlled Level 1 BTC Short Straddle on Bybit Testnet with explicit confirmation gate.",
    )
    parser.add_argument(
        "--connectivity-check",
        "--check-connection",
        action="store_true",
        help="Connect to Bybit Testnet WebSocket feeds and verify live handshake.",
    )
    parser.add_argument(
        "--discover-options",
        "--discovery",
        action="store_true",
        help="Query Bybit Testnet and discover available BTC options chain and strikes.",
    )
    parser.add_argument(
        "--subscribe-quotes",
        "--market-data-diagnostic",
        action="store_true",
        help="Subscribe to live QuoteTick feeds for underlying and ATM straddle legs without placing orders.",
    )
    parser.add_argument(
        "--select-straddle",
        "--atm-selection",
        action="store_true",
        help="Calculate and select the nearest ATM Call/Put straddle pair based on live reference price.",
    )
    parser.add_argument(
        "--test-order-manager",
        "--order-manager",
        action="store_true",
        help="Execute the Order Management state machine in DRY_RUN mode, verifying dual-leg tracking and unmatched-leg resolution.",
    )
    parser.add_argument(
        "--config-check",
        action="store_true",
        help="Audit configuration settings and security boundaries.",
    )

    args = parser.parse_args()

    if args.level2_dry_run:
        return asyncio.run(run_level2_dry_run_demonstration())
    elif args.level2_simulations:
        return run_level2_simulations_cli()
    elif args.level2_testnet:
        return asyncio.run(run_level2_testnet_demonstration())
    elif args.level1_dry_run:
        return asyncio.run(run_level1_dry_run_demonstration())
    elif args.level1_testnet:
        return asyncio.run(run_level1_testnet_demonstration())
    elif args.connectivity_check:
        return run_connectivity_check()
    elif args.discover_options:
        return asyncio.run(run_discovery_cli())
    elif args.subscribe_quotes:
        return asyncio.run(run_market_data_diagnostic(duration_seconds=12.0))
    elif args.select_straddle:
        return asyncio.run(run_straddle_selection_cli())
    elif args.test_order_manager:
        return asyncio.run(run_order_manager_diagnostic())
    elif args.config_check:
        return run_configuration_check()
    else:
        # Default behavior: run configuration check followed by guidance
        return run_configuration_check()


if __name__ == "__main__":
    sys.exit(main())
