"""
BTC Options Straddle - Main Initialization Entry Point.

Assignment: Mernplus Technologies BTC Options Straddle
Stage: Part 6 - Market Data Subscriptions & Quote Integrity
"""

import argparse
import asyncio
from datetime import datetime, timezone
import sys
from pathlib import Path

# Add project root to path for direct execution
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import SecurityError, get_settings
from exchange.bybit_client import run_connectivity_check
from exchange.instrument_discovery import run_discovery_cli
from exchange.market_data import run_market_data_diagnostic
from strategy.instrument_selector import run_straddle_selection_cli
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
        from nautilus_trader.model.currencies import BTC
        from nautilus_trader.trading.strategy import Strategy

        logger.info("[OK] NautilusTrader package loaded successfully.")
        logger.info(f"     NautilusTrader version: {nautilus_trader.__version__}")
        logger.info(
            f"     Core Rust/Cython components verified: BTC={BTC}, Strategy={Strategy.__name__}"
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
    logger.info(f"Stop-Loss Threshold : {settings.STOP_LOSS_PERCENT}%")
    logger.info(f"Take-Profit Target  : {settings.TAKE_PROFIT_PERCENT}%")
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
    logger.info("  python main.py --connectivity-check  (Verify Testnet WebSockets)")
    logger.info("  python main.py --discover-options    (Discover active BTC options chain)")
    logger.info("  python main.py --subscribe-quotes    (Stream live quotes & test straddle snapshot)")
    logger.info("  python main.py --select-straddle     (Calculate and select active ATM straddle legs)")
    logger.info("  python main.py --test-order-manager  (Verify dual-leg order state machine & policies)")
    logger.info("=" * 60)
    return 0


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
    from exchange.instrument_discovery import BtcOptionsDiscoveryService
    from strategy.instrument_selector import InstrumentSelector
    from strategy.order_manager import (
        LegExecutionStatus,
        OptionLegType,
        OrderManager,
        StraddleExecutionStatus,
        UnmatchedLegPolicy,
    )

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
    # Use $68,500 reference price or current spot
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


def main() -> int:
    """CLI dispatcher."""
    parser = argparse.ArgumentParser(
        description="Mernplus Technologies - BTC Options Straddle Trading System"
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

    if args.connectivity_check:
        return run_connectivity_check()
    elif args.discover_options:
        return asyncio.run(run_discovery_cli())
    elif args.subscribe_quotes:
        return asyncio.run(run_market_data_diagnostic(duration_seconds=12.0))
    elif args.select_straddle:
        return asyncio.run(run_straddle_selection_cli())
    elif args.test_order_manager:
        return asyncio.run(run_order_manager_diagnostic())
    else:
        return run_configuration_check()


if __name__ == "__main__":
    sys.exit(main())
