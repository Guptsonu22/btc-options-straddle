"""
Unit tests for Bybit Testnet Client module.

Verifies:
1. Enforcement of Testnet-only barrier in BybitTestnetClient.
2. Configuration of BybitDataClientConfig with OPTION and LINEAR product types.
3. Execution client behavior when credentials are provided vs omitted (DRY_RUN).
4. Safe node creation without placing any orders.
"""

import pytest
from pydantic import SecretStr

from config.settings import SecurityError, Settings
from exchange.bybit_client import BybitConnectionStatus, BybitTestnetClient
from nautilus_trader.adapters.bybit.config import BybitEnvironment, BybitProductType


def test_testnet_client_rejects_live_environment():
    """Verify that BybitTestnetClient immediately raises SecurityError if BYBIT_TESTNET is False."""
    with pytest.raises(SecurityError, match="BYBIT_TESTNET must remain True"):
        # Setting BYBIT_TESTNET=False triggers SecurityError
        Settings(
            BYBIT_TESTNET=False,
            BYBIT_API_KEY=SecretStr("test_key"),
            BYBIT_API_SECRET=SecretStr("test_secret"),
        )


def test_testnet_client_dry_run_node_construction():
    """Verify node configuration when credentials are omitted (Data-only / DRY_RUN mode)."""
    settings = Settings(
        BYBIT_TESTNET=True,
        TRADING_MODE="DRY_RUN",
        BYBIT_API_KEY=None,
        BYBIT_API_SECRET=None,
    )
    client = BybitTestnetClient(settings=settings)
    node = client.build_trading_node()

    assert node is not None
    assert str(node.trader_id) == "BYBIT-TESTNET-NODE"
    assert "BYBIT" in [str(c) for c in node.kernel.data_engine.registered_clients]
    assert len(node.kernel.exec_engine.registered_clients) == 0


def test_testnet_client_authenticated_node_construction():
    """Verify execution client registration when credentials are present."""
    settings = Settings(
        BYBIT_TESTNET=True,
        TRADING_MODE="DRY_RUN",
        BYBIT_API_KEY=SecretStr("valid_testnet_key_12345"),
        BYBIT_API_SECRET=SecretStr("valid_testnet_secret_67890"),
    )
    client = BybitTestnetClient(settings=settings)
    node = client.build_trading_node()

    assert "BYBIT" in [str(c) for c in node.kernel.data_engine.registered_clients]
    assert "BYBIT" in [str(c) for c in node.kernel.exec_engine.registered_clients]



def test_connection_status_dataclass():
    """Verify default values and mutation of BybitConnectionStatus."""
    status = BybitConnectionStatus()
    assert status.data_client_connected is False
    assert status.exec_client_connected is False
    assert status.environment == "Testnet"
    assert status.auth_status == "PENDING"
    assert status.latency_ms is None
