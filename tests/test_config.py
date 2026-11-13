"""
Unit Tests for BTC Options Straddle Configuration Management.

Verifies:
1. Safe defaults for all 13 configuration fields.
2. Separation of AppConfig from StrategyConfig.
3. Strict rejection of production/mainnet configurations (SecurityError).
4. Rejection of BYBIT_TESTNET=False.
5. Numerical validation of order quantity, stop-loss, take-profit, and timeouts.
6. Validation of UNMATCHED_LEG_POLICY enum.
7. Requirement of API credentials in PAPER mode.
8. Secret masking (secrets never leak in str, repr, or safe_summary).
"""

import os
import pytest
from pydantic import ValidationError
from config.settings import AppConfig, StrategyConfig, Settings, SecurityError


def test_default_settings_safe():
    """Verify that default settings initialize in a secure, simulated DRY_RUN state."""
    settings = Settings()

    # Application Config
    assert settings.BYBIT_TESTNET is True
    assert settings.TRADING_MODE == "DRY_RUN"
    assert settings.DRY_RUN is True
    assert settings.is_dry_run() is True
    assert settings.LOG_LEVEL == "INFO"
    assert settings.BYBIT_API_KEY is None
    assert settings.BYBIT_API_SECRET is None

    # Strategy Config
    assert settings.BTC_SYMBOL == "BTC"
    assert settings.ORDER_QUANTITY == 0.1
    assert settings.STOP_LOSS_PERCENT == 5.0
    assert settings.TAKE_PROFIT_PERCENT == 10.0
    assert settings.MAX_QUOTE_AGE_SECONDS == 5.0
    assert settings.ENTRY_TIMEOUT_SECONDS == 10.0
    assert settings.UNMATCHED_LEG_POLICY == "CANCEL_AND_CLOSE"


def test_separation_of_app_and_strategy_config():
    """Verify that Application configuration is structurally distinct from Strategy configuration."""
    settings = Settings()

    assert isinstance(settings.app, AppConfig)
    assert isinstance(settings.strategy, StrategyConfig)

    # Verify AppConfig isolates connectivity & safety
    assert hasattr(settings.app, "BYBIT_TESTNET")
    assert hasattr(settings.app, "TRADING_MODE")
    assert hasattr(settings.app, "DRY_RUN")
    assert not hasattr(settings.app, "STOP_LOSS_PERCENT")

    # Verify StrategyConfig isolates trading & quant rules
    assert hasattr(settings.strategy, "ORDER_QUANTITY")
    assert hasattr(settings.strategy, "STOP_LOSS_PERCENT")
    assert hasattr(settings.strategy, "TAKE_PROFIT_PERCENT")
    assert not hasattr(settings.strategy, "BYBIT_API_KEY")


def test_reject_production_trading_mode():
    """Verify that any attempt to set TRADING_MODE to LIVE or PRODUCTION is rejected."""
    with pytest.raises(SecurityError, match="Prohibited TRADING_MODE"):
        AppConfig(TRADING_MODE="LIVE")

    with pytest.raises(SecurityError, match="Prohibited TRADING_MODE"):
        AppConfig(TRADING_MODE="PRODUCTION")

    with pytest.raises(SecurityError, match="Prohibited TRADING_MODE"):
        Settings(TRADING_MODE="MAINNET")


def test_reject_testnet_disabled():
    """Verify that disabling BYBIT_TESTNET raises an immediate SecurityError."""
    with pytest.raises(SecurityError, match="BYBIT_TESTNET must remain True"):
        AppConfig(BYBIT_TESTNET=False)

    with pytest.raises(SecurityError, match="BYBIT_TESTNET must remain True"):
        Settings(BYBIT_TESTNET=False)


def test_reject_production_environment_variable(monkeypatch):
    """Verify that an ENVIRONMENT variable set to production triggers SecurityError."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(SecurityError, match="forbidden"):
        AppConfig()


def test_paper_trading_requires_credentials():
    """Verify that PAPER mode requires valid API credentials, while DRY_RUN does not."""
    # DRY_RUN without keys: Valid
    app_dry = AppConfig(TRADING_MODE="DRY_RUN")
    assert app_dry.DRY_RUN is True

    # PAPER mode without keys: Raises ValueError
    with pytest.raises(ValueError, match="BYBIT_API_KEY is required"):
        AppConfig(TRADING_MODE="PAPER")

    # PAPER mode with valid keys: Valid & DRY_RUN is False
    app_paper = AppConfig(
        TRADING_MODE="PAPER",
        BYBIT_API_KEY="testnet_key_abc",
        BYBIT_API_SECRET="testnet_secret_xyz",
    )
    assert app_paper.TRADING_MODE == "PAPER"
    assert app_paper.DRY_RUN is False


def test_validate_order_quantity():
    """Verify that ORDER_QUANTITY must be a positive number."""
    # Valid positive quantity
    strat = StrategyConfig(ORDER_QUANTITY=0.5)
    assert strat.ORDER_QUANTITY == 0.5

    # Zero quantity rejected
    with pytest.raises(ValidationError):
        StrategyConfig(ORDER_QUANTITY=0.0)

    # Negative quantity rejected
    with pytest.raises(ValidationError):
        StrategyConfig(ORDER_QUANTITY=-1.0)

    # Non-numeric rejected
    with pytest.raises(ValidationError):
        StrategyConfig(ORDER_QUANTITY="invalid_qty")


def test_validate_stop_loss_percent():
    """Verify that STOP_LOSS_PERCENT must be within (0.0, 100.0]."""
    # Valid bounds
    assert StrategyConfig(STOP_LOSS_PERCENT=1.0).STOP_LOSS_PERCENT == 1.0
    assert StrategyConfig(STOP_LOSS_PERCENT=100.0).STOP_LOSS_PERCENT == 100.0

    # Zero or negative rejected
    with pytest.raises(ValidationError):
        StrategyConfig(STOP_LOSS_PERCENT=0.0)
    with pytest.raises(ValidationError):
        StrategyConfig(STOP_LOSS_PERCENT=-5.0)

    # Greater than 100 rejected
    with pytest.raises(ValidationError):
        StrategyConfig(STOP_LOSS_PERCENT=105.0)


def test_validate_take_profit_percent():
    """Verify that TAKE_PROFIT_PERCENT must be strictly positive."""
    assert StrategyConfig(TAKE_PROFIT_PERCENT=25.0).TAKE_PROFIT_PERCENT == 25.0

    with pytest.raises(ValidationError):
        StrategyConfig(TAKE_PROFIT_PERCENT=0.0)
    with pytest.raises(ValidationError):
        StrategyConfig(TAKE_PROFIT_PERCENT=-10.0)


def test_validate_quote_age_and_timeout():
    """Verify positive constraints on MAX_QUOTE_AGE_SECONDS and ENTRY_TIMEOUT_SECONDS."""
    with pytest.raises(ValidationError):
        StrategyConfig(MAX_QUOTE_AGE_SECONDS=0.0)
    with pytest.raises(ValidationError):
        StrategyConfig(MAX_QUOTE_AGE_SECONDS=-2.0)

    with pytest.raises(ValidationError):
        StrategyConfig(ENTRY_TIMEOUT_SECONDS=0.0)
    with pytest.raises(ValidationError):
        StrategyConfig(ENTRY_TIMEOUT_SECONDS=-5.0)


def test_validate_unmatched_leg_policy():
    """Verify acceptable policies for UNMATCHED_LEG_POLICY."""
    for policy in ("CANCEL_AND_CLOSE", "CANCEL_REMAINING", "HEDGE_ORPHAN"):
        cfg = StrategyConfig(UNMATCHED_LEG_POLICY=policy)
        assert cfg.UNMATCHED_LEG_POLICY == policy

    with pytest.raises(ValidationError):
        StrategyConfig(UNMATCHED_LEG_POLICY="INVALID_POLICY")


def test_symbol_normalization():
    """Verify symbol normalization to trimmed uppercase."""
    cfg = StrategyConfig(BTC_SYMBOL=" btc ")
    assert cfg.BTC_SYMBOL == "BTC"

    with pytest.raises(ValidationError):
        StrategyConfig(BTC_SYMBOL="")


def test_credential_masking_never_leaks_secrets():
    """Verify that credentials are never exposed in string representations, repr, or safe summary."""
    secret_key = "sensitive_key_9999"
    secret_val = "sensitive_secret_8888"

    settings = Settings(
        BYBIT_API_KEY=secret_key,
        BYBIT_API_SECRET=secret_val,
        TRADING_MODE="DRY_RUN",
    )

    # 1. Repr masking
    repr_str = repr(settings)
    assert secret_key not in repr_str
    assert secret_val not in repr_str
    assert "[REDACTED]" in repr_str

    # 2. Str masking
    str_val = str(settings)
    assert secret_key not in str_val
    assert secret_val not in str_val

    # 3. Safe summary masking
    summary = settings.safe_summary()
    assert summary["BYBIT_API_KEY"] == "********"
    assert summary["BYBIT_API_SECRET"] == "********"
    assert secret_key not in str(summary)
    assert secret_val not in str(summary)
