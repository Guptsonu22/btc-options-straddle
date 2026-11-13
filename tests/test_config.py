"""
Unit tests for Configuration and Security Assertions.
"""

import pytest
from pydantic import ValidationError
from config.settings import Settings, SecurityError


def test_default_settings_safe():
    """Verify that default settings start in a secure, non-live state."""
    settings = Settings()
    assert settings.ENVIRONMENT == "testnet"
    assert settings.DERIBIT_TESTNET is True
    assert settings.TRADING_MODE == "DRY_RUN"
    assert settings.is_dry_run() is True


def test_refuse_production_environment():
    """Verify that any attempt to specify 'production' environment is immediately rejected."""
    with pytest.raises((SecurityError, ValidationError)):
        Settings(ENVIRONMENT="production")

    with pytest.raises((SecurityError, ValidationError)):
        Settings(ENVIRONMENT="mainnet")

    with pytest.raises((SecurityError, ValidationError)):
        Settings(ENVIRONMENT="live")


def test_refuse_testnet_disabled():
    """Verify that setting DERIBIT_TESTNET to False is blocked."""
    with pytest.raises((SecurityError, ValidationError)):
        Settings(DERIBIT_TESTNET=False)


def test_credentials_masked_in_safe_summary():
    """Verify that API credentials are never exposed in plaintext."""
    settings = Settings(
        DERIBIT_CLIENT_ID="secret_key_12345",
        DERIBIT_CLIENT_SECRET="ultra_secret_pass_67890",
    )
    summary = settings.safe_summary()
    assert summary["DERIBIT_CLIENT_ID"] == "********"
    assert summary["DERIBIT_CLIENT_SECRET"] == "********"
    assert "secret_key_12345" not in str(summary)
    assert "ultra_secret_pass_67890" not in str(summary)
