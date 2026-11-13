"""
Bybit Testnet Client Manager for NautilusTrader.

Implements connectivity management, authentication handling, and graceful shutdown
for Bybit Testnet Option & Linear market data and execution clients.

Zero orders are placed by this module. Connectivity-only verification.
"""

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nautilus_trader.adapters.bybit.config import (
    BybitDataClientConfig,
    BybitEnvironment,
    BybitExecClientConfig,
    BybitProductType,
)
from nautilus_trader.adapters.bybit.factories import (
    BybitLiveDataClientFactory,
    BybitLiveExecClientFactory,
)
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.live.node import TradingNode

from config.settings import SecurityError, Settings, get_settings
from utils.logger import get_logger, setup_logger


@dataclass
class BybitConnectionStatus:
    """Connection diagnostics record."""
    data_client_connected: bool = False
    exec_client_connected: bool = False
    environment: str = "Testnet"
    auth_status: str = "PENDING"
    error_message: Optional[str] = None
    latency_ms: Optional[float] = None


class BybitTestnetClient:
    """
    Manages Bybit Testnet connectivity using the official NautilusTrader 1.231.0 API.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.logger = get_logger("btc_straddle.bybit")
        self.node: Optional[TradingNode] = None
        self._run_task: Optional[asyncio.Task] = None

        # Enforce security gates
        if not self.settings.BYBIT_TESTNET:
            raise SecurityError("Execution aborted: BYBIT_TESTNET must be True.")

    def build_trading_node(self) -> TradingNode:
        """
        Configure and build the NautilusTrader TradingNode for Bybit Testnet.
        """
        api_key = (
            self.settings.BYBIT_API_KEY.get_secret_value()
            if self.settings.BYBIT_API_KEY
            else None
        )
        api_secret = (
            self.settings.BYBIT_API_SECRET.get_secret_value()
            if self.settings.BYBIT_API_SECRET
            else None
        )

        has_credentials = bool(api_key and api_secret and "your_bybit" not in api_key.lower())

        # 1. Bybit Market Data Client Configuration (Public WebSockets for Option & Linear)
        data_config = BybitDataClientConfig(
            api_key=api_key if has_credentials else None,
            api_secret=api_secret if has_credentials else None,
            environment=BybitEnvironment.TESTNET,
            product_types=(
                BybitProductType.OPTION,
                BybitProductType.LINEAR,
            ),
            update_instruments_interval_mins=60,
            recv_window_ms=5000,
        )

        data_clients = {"BYBIT": data_config}
        exec_clients = {}

        # 2. Bybit Execution Client Configuration (Requires API credentials)
        if has_credentials:
            exec_config = BybitExecClientConfig(
                api_key=api_key,
                api_secret=api_secret,
                environment=BybitEnvironment.TESTNET,
                product_types=(
                    BybitProductType.OPTION,
                    BybitProductType.LINEAR,
                ),
                use_ws_execution_fast=True,
                recv_window_ms=5000,
                ws_trade_timeout_secs=5.0,
            )
            exec_clients["BYBIT"] = exec_config
            self.logger.info("Credentials configured. Registering Bybit Execution Client.")
        else:
            self.logger.info(
                "No API credentials configured. Operating in public Data-Only Testnet mode "
                "(Authentication skipped; private execution client omitted)."
            )

        # 3. Node Configuration
        node_config = TradingNodeConfig(
            trader_id="BYBIT-TESTNET-NODE",
            data_clients=data_clients,
            exec_clients=exec_clients,
            timeout_connection=10.0,
            timeout_reconciliation=10.0,
            timeout_disconnection=10.0,
        )

        # 4. Initialize Node & Register Factories
        node = TradingNode(config=node_config)
        node.add_data_client_factory("BYBIT", BybitLiveDataClientFactory)

        if has_credentials:
            node.add_exec_client_factory("BYBIT", BybitLiveExecClientFactory)

        # 5. Build Internal Topology
        node.build()
        self.node = node
        self.logger.info("NautilusTrader TradingNode built successfully.")
        return node

    async def connect_and_verify(
        self,
        wait_seconds: float = 6.0,
    ) -> BybitConnectionStatus:
        """
        Connect both clients, monitor connection state, and gracefully disconnect.

        Parameters
        ----------
        wait_seconds : float
            Time in seconds to wait for WebSocket handshakes.

        Returns
        -------
        BybitConnectionStatus
            Status object containing verified connection metrics.
        """
        status = BybitConnectionStatus()
        start_time = time.monotonic()

        if self.node is None:
            self.build_trading_node()

        node = self.node
        assert node is not None

        self.logger.info("Starting Bybit Testnet connection handshake...")
        # Launch node run_async in background task
        self._run_task = asyncio.create_task(node.run_async())

        try:
            # Poll connection status over the timeout window
            poll_interval = 0.5
            max_iterations = int(wait_seconds / poll_interval)

            for i in range(max_iterations):
                await asyncio.sleep(poll_interval)

                data_connected = False
                exec_connected = False

                if node.kernel.data_engine:
                    data_connected = node.kernel.data_engine.check_connected()

                if node.kernel.exec_engine and node.kernel.exec_engine._clients:
                    exec_connected = node.kernel.exec_engine.check_connected()

                status.data_client_connected = data_connected
                status.exec_client_connected = exec_connected

                if data_connected:
                    elapsed = (time.monotonic() - start_time) * 1000
                    status.latency_ms = round(elapsed, 2)
                    self.logger.info(
                        f"[CONNECTED] Bybit Testnet Data Client is active (latency: {status.latency_ms}ms)."
                    )
                    break

            # Check execution client status
            if node.kernel.exec_engine and node.kernel.exec_engine._clients:
                if status.exec_client_connected:
                    status.auth_status = "AUTHENTICATED"
                    self.logger.info("[AUTHENTICATED] Bybit Testnet Execution Client is active.")
                else:
                    status.auth_status = "AUTH_FAILED_OR_PENDING"
                    self.logger.warning(
                        "[WARN] Execution Client connection pending or authentication rejected."
                    )
            else:
                status.auth_status = "SKIPPED_DRY_RUN"
                self.logger.info("Private execution client omitted (DRY_RUN / credentials omitted).")

        except Exception as e:
            status.error_message = str(e)
            self.logger.error(f"[CONNECTION ERROR] Handshake failed: {e}")
        finally:
            # Always ensure graceful shutdown
            await self.shutdown()

        return status

    async def shutdown(self) -> None:
        """Gracefully disconnect all clients and dispose of node resources."""
        if self.node and self.node.is_running():
            self.logger.info("Initiating graceful disconnect from Bybit Testnet...")
            try:
                await self.node.stop_async()
                self.logger.info("All Bybit Testnet clients disconnected safely.")
            except Exception as e:
                self.logger.warning(f"Notice during shutdown: {e}")

        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass

        self.node = None


def run_connectivity_check() -> int:
    """Synchronous entry point for the connectivity-only test."""
    logger = setup_logger("btc_straddle.bybit")
    logger.info("=" * 65)
    logger.info("BYBIT TESTNET CONNECTIVITY VERIFICATION (PART 4)")
    logger.info("=" * 65)

    client = BybitTestnetClient()
    status = asyncio.run(client.connect_and_verify(wait_seconds=6.0))

    logger.info("-" * 65)
    logger.info("CONNECTIVITY DIAGNOSTIC RESULTS:")
    logger.info(f"Target Venue            : Bybit {status.environment}")
    logger.info(f"Data Client Connected   : {status.data_client_connected}")
    logger.info(f"Exec Client Connected   : {status.exec_client_connected}")
    logger.info(f"Authentication Status   : {status.auth_status}")
    if status.latency_ms:
        logger.info(f"Handshake Roundtrip     : {status.latency_ms} ms")
    if status.error_message:
        logger.info(f"Encountered Error       : {status.error_message}")
    logger.info("-" * 65)

    if status.data_client_connected:
        logger.info("[SUCCESS] Bybit Testnet WebSocket handshake verified successfully!")
        logger.info("System disconnected cleanly with zero orders placed.")
        logger.info("=" * 65)
        return 0
    else:
        logger.error("[FAIL] Bybit Testnet connection could not be established.")
        logger.info("=" * 65)
        return 1


if __name__ == "__main__":
    sys.exit(run_connectivity_check())
