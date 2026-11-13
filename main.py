"""
BTC Options Straddle - Main Initialization Entry Point.

Assignment: Mernplus Technologies BTC Options Straddle
Stage: Part 5 - Options Discovery & Safe Architecture
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path for direct execution
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import SecurityError, get_settings
from exchange.bybit_client import run_connectivity_check
from exchange.instrument_discovery import run_discovery_cli
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
    logger.info("=" * 60)
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
        "--config-check",
        action="store_true",
        help="Audit configuration settings and security boundaries.",
    )

    args = parser.parse_args()

    if args.connectivity_check:
        return run_connectivity_check()
    elif args.discover_options:
        return asyncio.run(run_discovery_cli())
    else:
        return run_configuration_check()


if __name__ == "__main__":
    sys.exit(main())
