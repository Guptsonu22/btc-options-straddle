"""
Structured and Secure Logging Utility.

Ensures clear execution tracing while strictly preventing secrets (like API keys)
from ever appearing in terminal logs or disk log files.
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SensitiveDataFilter(logging.Filter):
    """
    Filter that scrubs potential secrets, API keys, and sensitive tokens
    from log records before output.
    """

    PATTERNS = [
        (re.compile(r"(client_secret[=:\s]+)([^\s,]+)", re.IGNORECASE), r"\1[REDACTED]"),
        (re.compile(r"(client_id[=:\s]+)([^\s,]+)", re.IGNORECASE), r"\1[REDACTED]"),
        (re.compile(r"(api[_-]?key[=:\s]+)([^\s,]+)", re.IGNORECASE), r"\1[REDACTED]"),
        (re.compile(r"(password[=:\s]+)([^\s,]+)", re.IGNORECASE), r"\1[REDACTED]"),
        (re.compile(r"(Bearer\s+)[^\s,]+", re.IGNORECASE), r"\1[REDACTED]"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            for pattern, replacement in self.PATTERNS:
                record.msg = pattern.sub(replacement, record.msg)
        return True


def setup_logger(
    name: str = "btc_straddle",
    log_level: str = "INFO",
    log_to_console: bool = True,
    log_file_path: Optional[str] = "logs/straddle_engine.log",
) -> logging.Logger:
    """
    Configure and return a standardized logger with security redactions.

    Parameters
    ----------
    name : str
        The name of the logger instance.
    log_level : str
        Logging verbosity (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    log_to_console : bool
        Whether to stream log records to stderr/stdout.
    log_file_path : Optional[str]
        Relative or absolute path for file output. If None, file logging is disabled.

    Returns
    -------
    logging.Logger
        Configured logger instance.
    """
    logger = logging.getLogger(name)
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger.setLevel(level)

    # Avoid duplicate handlers if already configured
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s:%(filename)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    secret_filter = SensitiveDataFilter()

    if log_to_console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(secret_filter)
        logger.addHandler(console_handler)

    if log_file_path:
        log_path = Path(log_file_path)
        if not log_path.is_absolute():
            log_path = PROJECT_ROOT / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(secret_filter)
        logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "btc_straddle") -> logging.Logger:
    """Retrieve an existing logger or initialize default."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger
