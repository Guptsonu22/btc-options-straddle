"""
Configuration Management for BTC Options Straddle.

Architecture:
- AppConfig: Application, exchange connectivity, environment, and security gates.
- StrategyConfig: Quantitative parameters, order sizing, risk thresholds, and execution timeouts.
- Settings: Unified root settings aggregating AppConfig and StrategyConfig.

Security Principles:
1. Production Refusal: Strictly rejects production/mainnet configurations.
2. Testnet Enforcement: BYBIT_TESTNET must remain True.
3. Secret Protection: Credentials wrapped in SecretStr; never exposed in repr or logs.
4. Input Validation: Strict bounds on percentages, quantities, freshness, and timeouts.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Locate project root and default .env file path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE_PATH = PROJECT_ROOT / ".env"


class SecurityError(RuntimeError):
    """Raised when a prohibited production configuration or security violation is detected."""
    pass


class AppConfig(BaseSettings):
    """
    Application, network, and exchange connectivity configuration.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Bybit API Credentials (Wrapped in SecretStr to prevent plaintext exposure)
    BYBIT_API_KEY: Optional[SecretStr] = Field(
        default=None,
        description="Bybit Testnet API Public Key. Optional in DRY_RUN mode, required in PAPER mode.",
    )

    BYBIT_API_SECRET: Optional[SecretStr] = Field(
        default=None,
        description="Bybit Testnet API Secret Key. Optional in DRY_RUN mode, required in PAPER mode.",
    )

    # Network & Safety Flags
    BYBIT_TESTNET: bool = Field(
        default=True,
        description="Enforces Bybit Testnet network endpoint. Must ALWAYS be True.",
    )

    TRADING_MODE: Literal["DRY_RUN", "PAPER"] = Field(
        default="DRY_RUN",
        description="Execution mode: 'DRY_RUN' (local simulated orders) or 'PAPER' (Deribit/Bybit Testnet).",
    )

    DRY_RUN: bool = Field(
        default=True,
        description="Flag indicating if orders should be simulated without sending to venue.",
    )

    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="System logging level.",
    )

    # --------------------------------------------------------------------------
    # Security Validators
    # --------------------------------------------------------------------------
    @field_validator("BYBIT_TESTNET")
    @classmethod
    def validate_testnet_only(cls, v: bool) -> bool:
        """Enforce Testnet-only trading; reject any attempt to disable it."""
        if not v:
            raise SecurityError(
                "CRITICAL SECURITY ERROR: Mainnet / Live trading is strictly prohibited! "
                "BYBIT_TESTNET must remain True."
            )
        return v

    @field_validator("TRADING_MODE", mode="before")
    @classmethod
    def validate_trading_mode(cls, v: Any) -> str:
        """Reject production/live trading mode designations."""
        val = str(v).strip().upper()
        if val in ("LIVE", "PRODUCTION", "PROD", "MAINNET"):
            raise SecurityError(
                f"CRITICAL SECURITY ERROR: Prohibited TRADING_MODE='{val}' detected! "
                "This engine only permits 'DRY_RUN' and 'PAPER' on Testnet."
            )
        if val not in ("DRY_RUN", "PAPER"):
            raise ValueError(f"Invalid TRADING_MODE: '{val}'. Allowed values: 'DRY_RUN', 'PAPER'.")
        return val

    @model_validator(mode="after")
    def validate_app_integrity(self) -> "AppConfig":
        """Enforce internal consistency and credential presence in PAPER mode."""
        # 1. Check system environment for dangerous production flags
        env_val = os.getenv("ENVIRONMENT", "").strip().lower()
        if env_val in ("production", "prod", "mainnet", "live"):
            raise SecurityError(
                f"CRITICAL SECURITY ERROR: Environment variable ENVIRONMENT='{env_val}' is forbidden! "
                "Production execution is rejected by policy."
            )

        # 2. Synchronize DRY_RUN flag with TRADING_MODE
        if self.TRADING_MODE == "DRY_RUN":
            self.DRY_RUN = True
        elif self.TRADING_MODE == "PAPER":
            self.DRY_RUN = False
            # In PAPER mode, credentials must be present
            if not self.BYBIT_API_KEY or not self.BYBIT_API_KEY.get_secret_value().strip():
                raise ValueError("BYBIT_API_KEY is required and cannot be empty when TRADING_MODE is 'PAPER'.")
            if not self.BYBIT_API_SECRET or not self.BYBIT_API_SECRET.get_secret_value().strip():
                raise ValueError("BYBIT_API_SECRET is required and cannot be empty when TRADING_MODE is 'PAPER'.")

        return self

    def __repr__(self) -> str:
        return (
            f"AppConfig(BYBIT_TESTNET={self.BYBIT_TESTNET}, TRADING_MODE='{self.TRADING_MODE}', "
            f"DRY_RUN={self.DRY_RUN}, LOG_LEVEL='{self.LOG_LEVEL}', "
            f"BYBIT_API_KEY=[REDACTED], BYBIT_API_SECRET=[REDACTED])"
        )

    def __str__(self) -> str:
        return self.__repr__()


class StrategyConfig(BaseSettings):
    """
    Quantitative trading, risk, and order execution parameters.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    BTC_SYMBOL: str = Field(
        default="BTC",
        min_length=1,
        description="Underlying cryptocurrency symbol (e.g., 'BTC').",
    )

    ORDER_QUANTITY: float = Field(
        default=0.1,
        gt=0.0,
        description="Order quantity in BTC contracts per straddle leg. Must be positive.",
    )

    STOP_LOSS_PERCENT: float = Field(
        default=5.0,
        gt=0.0,
        le=100.0,
        description="Stop-loss threshold as a percentage of entry premium (0 < SL <= 100).",
    )

    TAKE_PROFIT_PERCENT: float = Field(
        default=10.0,
        gt=0.0,
        description="Take-profit threshold as a percentage of entry premium (TP > 0).",
    )

    MAX_QUOTE_AGE_SECONDS: float = Field(
        default=5.0,
        gt=0.0,
        description="Maximum acceptable age of market quote in seconds before it is rejected as stale.",
    )

    ENTRY_TIMEOUT_SECONDS: float = Field(
        default=10.0,
        gt=0.0,
        description="Maximum seconds to wait for dual-leg order execution before canceling.",
    )

    UNMATCHED_LEG_POLICY: Literal["CANCEL_AND_CLOSE", "CANCEL_REMAINING", "HEDGE_ORPHAN"] = Field(
        default="CANCEL_AND_CLOSE",
        description="Policy to execute if one straddle leg fills and the opposite leg fails or times out.",
    )

    @field_validator("BTC_SYMBOL")
    @classmethod
    def normalize_symbol(cls, v: str) -> str:
        """Normalize BTC symbol to uppercase trimmed string."""
        symbol = str(v).strip().upper()
        if not symbol:
            raise ValueError("BTC_SYMBOL cannot be empty.")
        return symbol

    def __repr__(self) -> str:
        return (
            f"StrategyConfig(BTC_SYMBOL='{self.BTC_SYMBOL}', ORDER_QUANTITY={self.ORDER_QUANTITY}, "
            f"STOP_LOSS_PERCENT={self.STOP_LOSS_PERCENT}%, TAKE_PROFIT_PERCENT={self.TAKE_PROFIT_PERCENT}%, "
            f"MAX_QUOTE_AGE_SECONDS={self.MAX_QUOTE_AGE_SECONDS}s, "
            f"ENTRY_TIMEOUT_SECONDS={self.ENTRY_TIMEOUT_SECONDS}s, "
            f"UNMATCHED_LEG_POLICY='{self.UNMATCHED_LEG_POLICY}')"
        )

    def __str__(self) -> str:
        return self.__repr__()


class Settings(BaseSettings):
    """
    Unified Application and Strategy Configuration Container.
    Aggregates AppConfig and StrategyConfig while exposing backward-compatible properties.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app: AppConfig = Field(default_factory=AppConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)

    def __init__(self, **kwargs: Any) -> None:
        """
        Allow initializing Settings either with nested dicts or flat keyword arguments.
        """
        app_fields = AppConfig.model_fields.keys()
        strat_fields = StrategyConfig.model_fields.keys()

        app_kwargs = {}
        strat_kwargs = {}

        if "app" in kwargs and isinstance(kwargs["app"], AppConfig):
            app_kwargs = kwargs["app"].model_dump()
        if "strategy" in kwargs and isinstance(kwargs["strategy"], StrategyConfig):
            strat_kwargs = kwargs["strategy"].model_dump()

        for k, v in kwargs.items():
            if k in app_fields:
                app_kwargs[k] = v
            elif k in strat_fields:
                strat_kwargs[k] = v

        super().__init__(
            app=AppConfig(**app_kwargs),
            strategy=StrategyConfig(**strat_kwargs),
        )

    # --------------------------------------------------------------------------
    # Delegation Properties (for direct access: settings.BYBIT_API_KEY, etc.)
    # --------------------------------------------------------------------------
    @property
    def BYBIT_API_KEY(self) -> Optional[SecretStr]:
        return self.app.BYBIT_API_KEY

    @property
    def BYBIT_API_SECRET(self) -> Optional[SecretStr]:
        return self.app.BYBIT_API_SECRET

    @property
    def BYBIT_TESTNET(self) -> bool:
        return self.app.BYBIT_TESTNET

    @property
    def TRADING_MODE(self) -> str:
        return self.app.TRADING_MODE

    @property
    def DRY_RUN(self) -> bool:
        return self.app.DRY_RUN

    @property
    def LOG_LEVEL(self) -> str:
        return self.app.LOG_LEVEL

    @property
    def BTC_SYMBOL(self) -> str:
        return self.strategy.BTC_SYMBOL

    @property
    def ORDER_QUANTITY(self) -> float:
        return self.strategy.ORDER_QUANTITY

    @property
    def STOP_LOSS_PERCENT(self) -> float:
        return self.strategy.STOP_LOSS_PERCENT

    @property
    def TAKE_PROFIT_PERCENT(self) -> float:
        return self.strategy.TAKE_PROFIT_PERCENT

    @property
    def MAX_QUOTE_AGE_SECONDS(self) -> float:
        return self.strategy.MAX_QUOTE_AGE_SECONDS

    @property
    def ENTRY_TIMEOUT_SECONDS(self) -> float:
        return self.strategy.ENTRY_TIMEOUT_SECONDS

    @property
    def UNMATCHED_LEG_POLICY(self) -> str:
        return self.strategy.UNMATCHED_LEG_POLICY

    def is_dry_run(self) -> bool:
        """Return True if running in DRY_RUN simulation mode."""
        return self.app.DRY_RUN

    def safe_summary(self) -> dict[str, Any]:
        """
        Return a sanitized dictionary suitable for logging and display,
        guaranteeing credentials remain completely masked.
        """
        return {
            # Application Config
            "BYBIT_TESTNET": self.app.BYBIT_TESTNET,
            "TRADING_MODE": self.app.TRADING_MODE,
            "DRY_RUN": self.app.DRY_RUN,
            "LOG_LEVEL": self.app.LOG_LEVEL,
            "BYBIT_API_KEY": "********" if self.app.BYBIT_API_KEY else None,
            "BYBIT_API_SECRET": "********" if self.app.BYBIT_API_SECRET else None,
            # Strategy Config
            "BTC_SYMBOL": self.strategy.BTC_SYMBOL,
            "ORDER_QUANTITY": self.strategy.ORDER_QUANTITY,
            "STOP_LOSS_PERCENT": self.strategy.STOP_LOSS_PERCENT,
            "TAKE_PROFIT_PERCENT": self.strategy.TAKE_PROFIT_PERCENT,
            "MAX_QUOTE_AGE_SECONDS": self.strategy.MAX_QUOTE_AGE_SECONDS,
            "ENTRY_TIMEOUT_SECONDS": self.strategy.ENTRY_TIMEOUT_SECONDS,
            "UNMATCHED_LEG_POLICY": self.strategy.UNMATCHED_LEG_POLICY,
        }

    def __repr__(self) -> str:
        return f"Settings(app={self.app!r}, strategy={self.strategy!r})"

    def __str__(self) -> str:
        return self.__repr__()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return cached application settings singleton.
    """
    return Settings()
