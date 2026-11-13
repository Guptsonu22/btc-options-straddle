"""
Configuration Module for BTC Options Straddle.

Implements strict security boundaries:
1. Refuses any production / mainnet configuration.
2. Requires DERIBIT_TESTNET=True.
3. Enforces DRY_RUN / PAPER trading mode.
4. Uses SecretStr for credentials to prevent memory/log leakage.
5. Loads parameters exclusively via environment variables or .env file.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecurityError(RuntimeError):
    """Raised whenever a dangerous or prohibited trading environment configuration is detected."""
    pass


class Settings(BaseSettings):
    """
    Application Settings with strict validation for options straddle execution.
    """

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --------------------------------------------------------------------------
    # Environment & Safety Gates
    # --------------------------------------------------------------------------
    ENVIRONMENT: Literal["testnet", "development", "test"] = Field(
        default="testnet",
        description="Target runtime environment. 'production' is strictly rejected.",
    )

    TRADING_MODE: Literal["DRY_RUN", "PAPER"] = Field(
        default="DRY_RUN",
        description="Execution mode. DRY_RUN simulates fills locally; PAPER routes to testnet.",
    )

    DERIBIT_TESTNET: bool = Field(
        default=True,
        description="Deribit network flag. Must ALWAYS be True.",
    )

    # --------------------------------------------------------------------------
    # Deribit Testnet Credentials (Encapsulated as SecretStr)
    # --------------------------------------------------------------------------
    DERIBIT_CLIENT_ID: Optional[SecretStr] = Field(
        default=None,
        description="Deribit Testnet API Client ID.",
    )

    DERIBIT_CLIENT_SECRET: Optional[SecretStr] = Field(
        default=None,
        description="Deribit Testnet API Client Secret.",
    )

    DERIBIT_TESTNET_WS_URL: str = Field(
        default="wss://test.deribit.com/ws/api/v2",
        description="Deribit Testnet WebSocket Endpoint.",
    )

    DERIBIT_TESTNET_HTTP_URL: str = Field(
        default="https://test.deribit.com/api/v2",
        description="Deribit Testnet JSON-RPC HTTP Endpoint.",
    )

    # --------------------------------------------------------------------------
    # Straddle Strategy Defaults
    # --------------------------------------------------------------------------
    BTC_STRADDLE_QUANTITY: float = Field(
        default=0.1,
        gt=0.0,
        description="Order quantity in BTC contracts for straddle legs.",
    )

    MAX_DRAWDOWN_PCT: float = Field(
        default=5.0,
        gt=0.0,
        le=100.0,
        description="Maximum portfolio drawdown percentage before automated stop-out.",
    )

    RISK_FREE_RATE: float = Field(
        default=0.04,
        ge=0.0,
        description="Annualized risk-free interest rate used in pricing models.",
    )

    # --------------------------------------------------------------------------
    # Logging Configuration
    # --------------------------------------------------------------------------
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_TO_CONSOLE: bool = True
    LOG_TO_FILE: bool = True
    LOG_FILE_PATH: str = "logs/straddle_engine.log"

    # --------------------------------------------------------------------------
    # Security Validators
    # --------------------------------------------------------------------------
    @field_validator("ENVIRONMENT", mode="before")
    @classmethod
    def validate_not_production(cls, v: str) -> str:
        """Reject any attempt to configure production."""
        val = str(v).strip().lower()
        if val in ("production", "prod", "mainnet", "live"):
            raise SecurityError(
                "CRITICAL SECURITY REJECTION: Production trading configuration detected! "
                "This system refuses production trading by policy. Only Testnet/Dry-Run is permitted."
            )
        return val

    @field_validator("DERIBIT_TESTNET")
    @classmethod
    def validate_testnet_flag(cls, v: bool) -> bool:
        """Refuse disabling the testnet flag."""
        if not v:
            raise SecurityError(
                "CRITICAL SECURITY REJECTION: DERIBIT_TESTNET was set to False! "
                "Mainnet/live trading is disabled. DERIBIT_TESTNET must remain True."
            )
        return v

    @model_validator(mode="after")
    def verify_overall_safety(self) -> "Settings":
        """Cross-field security integrity assertion."""
        if self.ENVIRONMENT in ("production", "prod", "mainnet", "live"):
            raise SecurityError("Refusing execution in production environment.")
        if not self.DERIBIT_TESTNET:
            raise SecurityError("Refusing execution outside Deribit Testnet.")
        return self

    def is_dry_run(self) -> bool:
        """Helper to determine whether orders should be purely simulated."""
        return self.TRADING_MODE == "DRY_RUN"

    def safe_summary(self) -> dict:
        """
        Return a dictionary of configuration safe for logging or printing,
        guaranteeing credentials remain completely masked.
        """
        data = self.model_dump()
        data["DERIBIT_CLIENT_ID"] = "********" if self.DERIBIT_CLIENT_ID else None
        data["DERIBIT_CLIENT_SECRET"] = "********" if self.DERIBIT_CLIENT_SECRET else None
        return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return cached application settings singleton.
    """
    return Settings()
