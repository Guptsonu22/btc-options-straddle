"""
BTC Options Straddle - Main Initialization Entry Point.

Assignment: Mernplus Technologies BTC Options Straddle
Stage: Part 1 - Project Initialization & Environment Setup
"""

import sys
from pathlib import Path

# Add project root to path for direct execution
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import get_settings, SecurityError
from utils.logger import setup_logger


def main() -> int:
    """Execute initial environment & security validation."""
    # 1. Initialize secure logging
    logger = setup_logger()
    logger.info("=" * 60)
    logger.info("Initializing BTC Options Straddle Engine (Part 1)")
    logger.info("=" * 60)

    # 2. Verify NautilusTrader availability & version
    try:
        import nautilus_trader
        from nautilus_trader.model.currencies import BTC
        from nautilus_trader.trading.strategy import Strategy
        logger.info(f"[OK] NautilusTrader package loaded successfully.")
        logger.info(f"     NautilusTrader version: {nautilus_trader.__version__}")
        logger.info(f"     Core Rust/Cython components verified: BTC={BTC}, Strategy={Strategy.__name__}")
    except ImportError as e:
        logger.error(f"[X] Failed to import NautilusTrader: {e}")
        return 1

    # 3. Load & Validate Configuration
    try:
        settings = get_settings()
        logger.info("[OK] Configuration loaded and validated successfully.")
    except SecurityError as sec_err:
        logger.critical(f"[SECURITY BLOCKED] {sec_err}")
        return 2
    except Exception as e:
        logger.error(f"[X] Configuration error: {e}")
        return 3

    # 4. Display Safety and Security Barriers
    logger.info("--- Security & Operational Checks ---")
    logger.info(f"Runtime Environment : {settings.ENVIRONMENT.upper()} (Enforced)")
    logger.info(f"Deribit Testnet     : {settings.DERIBIT_TESTNET} (Live trading disabled)")
    logger.info(f"Trading Mode        : {settings.TRADING_MODE} (Dry run armed)")
    logger.info(f"Straddle Quantity   : {settings.BTC_STRADDLE_QUANTITY} BTC")
    logger.info(f"Max Drawdown Limit  : {settings.MAX_DRAWDOWN_PCT}%")
    logger.info(f"Credential Protection: Verified (SecretStr masking active)")

    logger.info("Safe Config Dump:")
    for key, val in settings.safe_summary().items():
        logger.info(f"  {key}: {val}")

    logger.info("=" * 60)
    logger.info("PART 1 COMPLETE: Project initialized and verified.")
    logger.info("Exchange connectivity deferred to Part 2 as instructed.")
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
