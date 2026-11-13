# BTC Options Straddle Trading Engine

An institutional-grade, delta-neutral algorithmic trading engine built for Bitcoin (BTC) options straddle strategies using [NautilusTrader](https://nautilustrader.io/), designed for the **Mernplus Technologies BTC Options Straddle** assignment.

---

## Table of Contents
1. [Overview](#overview)
2. [Project Structure](#project-structure)
3. [Security & Risk Control Standards](#security--risk-control-standards)
4. [Prerequisites & System Specifications](#prerequisites--system-specifications)
5. [Installation & Setup (Step-by-Step)](#installation--setup-step-by-step)
6. [Configuration Guide](#configuration-guide)
7. [Running the Initialization Check](#running-the-initialization-check)
8. [Running Unit Tests](#running-unit-tests)
9. [Detailed File Breakdown](#detailed-file-breakdown)
10. [Roadmap to Part 2](#roadmap-to-part-2)

---

## Overview

A **Straddle** is an options strategy where an investor simultaneously buys (Long Straddle) or sells (Short Straddle) a Call option and a Put option with the exact same strike price and expiration date. 
- A **Long Straddle** profits when the underlying asset experiences large directional movement in *either* direction (high volatility).
- A **Short Straddle** profits when the asset price stays stable near the strike price (volatility collapse).

This project integrates high-performance Rust-backed components from NautilusTrader with Python to handle market data streaming, order state management, and algorithmic execution on Deribit Testnet.

---

## Project Structure

```
btc-options-straddle/
│
├── config/                 # Configuration module and security guardrails
│   ├── __init__.py         # Package exports (Settings, SecurityError, get_settings)
│   └── settings.py         # Pydantic-based settings with production rejection gates
│
├── strategy/               # Algorithmic straddle logic (Part 3/4)
│   └── __init__.py         # Strategy package initialization
│
├── exchange/               # Exchange integration & data feeds (Part 2)
│   └── __init__.py         # Exchange package initialization
│
├── utils/                  # Shared utilities and logging
│   ├── __init__.py         # Package exports
│   └── logger.py           # Structured logger with automatic secret redaction
│
├── tests/                  # Automated unit and integration test suite
│   ├── __init__.py         # Test package initialization
│   └── test_config.py      # Tests for configuration, security gates, and secret masking
│
├── logs/                   # Log directory (auto-created; gitignored)
│   └── .gitkeep            # Preserves directory in version control
│
├── main.py                 # Application initialization entry point
├── requirements.txt        # Pinned project dependencies
├── .env.example            # Environment variables template (safe defaults)
├── .gitignore              # Files excluded from version control
└── README.md               # Beginner-friendly documentation and guide
```

---

## Security & Risk Control Standards

Financial trading systems require rigorous defense-in-depth safety controls:

1. **Zero Hardcoded Secrets**: No API keys, passwords, or secrets are ever embedded in source code.
2. **Environment Isolation**: Credentials must be supplied via local `.env` file (which is strictly gitignored) or system environment variables.
3. **Pydantic `SecretStr`**: Credentials are held as opaque types in memory to prevent accidental exposure during logging, string formatting, or exceptions.
4. **Active Log Redaction**: The custom `SensitiveDataFilter` in `utils/logger.py` regex-scrubs any API keys or tokens before writing to console or file.
5. **Mandatory Testnet**: The system configuration enforces `DERIBIT_TESTNET=True`. Attempting to disable this flag raises an immediate `SecurityError`.
6. **Hard Prohibitions on Production**: If `ENVIRONMENT` is set to `production`, `prod`, `mainnet`, or `live`, the system refuses to run and aborts immediately.
7. **Default `DRY_RUN` Mode**: Orders are simulated in-memory by default so that testing never accidentally places live exchange orders.

---

## Prerequisites & System Specifications

- **Operating System**: Microsoft Windows 11 (64-bit) / Linux / macOS
- **Python Version**: Python 3.13 or 3.14 (64-bit)
- **Version Control**: Git 2.40+
- **Core Engine**: NautilusTrader 1.231.0

---

## Installation & Setup (Step-by-Step)

Follow these beginner-friendly instructions in PowerShell:

### Step 1: Open PowerShell and Navigate to the Project Root
```powershell
cd "c:\Users\Sonu\OneDrive\Desktop\BTC Options Straddle\btc-options-straddle"
```

### Step 2: Create a Python Virtual Environment
A virtual environment keeps project dependencies isolated from the rest of your computer:
```powershell
# Using Python 3.13
& "C:\Python313\python.exe" -m venv .venv
```
*Why this command?*
- `&` tells PowerShell to run the executable quoted in the path.
- `-m venv .venv` invokes Python's standard `venv` module to create a local environment folder named `.venv`.

### Step 3: Activate the Virtual Environment
```powershell
.\.venv\Scripts\Activate.ps1
```
*(If PowerShell shows an execution policy error, enable local scripts once via `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`)*

### Step 4: Upgrade Pip and Install Dependencies
```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```
*Why this command?*
- `pip install -r requirements.txt` reads every dependency in `requirements.txt` and downloads the pre-built, high-speed binary wheels.

### Step 5: Configure Your Local Environment File
```powershell
Copy-Item .env.example .env
```
Open `.env` in your editor and enter your Deribit Testnet credentials when ready.

---

## Configuration Guide

The engine reads settings through `config/settings.py` via Pydantic:

| Variable | Default | Purpose | Security Rule |
| :--- | :--- | :--- | :--- |
| `ENVIRONMENT` | `testnet` | Runtime tier | **Never** set to `production` |
| `TRADING_MODE` | `DRY_RUN` | Order simulation mode | Permitted: `DRY_RUN` or `PAPER` |
| `DERIBIT_TESTNET` | `true` | Deribit network switch | **Must** remain `true` |
| `DERIBIT_CLIENT_ID` | `None` | Testnet client key | Wrapped as `SecretStr` |
| `DERIBIT_CLIENT_SECRET` | `None` | Testnet client secret | Wrapped as `SecretStr` |
| `BTC_STRADDLE_QUANTITY` | `0.1` | Straddle leg size in BTC | Must be positive float |
| `MAX_DRAWDOWN_PCT` | `5.0` | Maximum drawdown limit (%) | Risk threshold stop |

---

## Running the Initialization Check

Run the entry point to verify all imports, Cython/Rust bindings, and security validators:

```powershell
python main.py
```

Expected output:
```
============================================================
Initializing BTC Options Straddle Engine (Part 1)
============================================================
[OK] NautilusTrader package loaded successfully.
     NautilusTrader version: 1.231.0
     Nautilus Rust/PyO3 core: 1.231.0
[OK] Configuration loaded and validated successfully.
--- Security & Operational Checks ---
Runtime Environment : TESTNET (Enforced)
Deribit Testnet     : True (Live trading disabled)
Trading Mode        : DRY_RUN (Dry run armed)
Straddle Quantity   : 0.1 BTC
Max Drawdown Limit  : 5.0%
Credential Protection: Verified (SecretStr masking active)
============================================================
PART 1 COMPLETE: Project initialized and verified.
Exchange connectivity deferred to Part 2 as instructed.
============================================================
```

---

## Running Unit Tests

Execute the automated test suite using `pytest`:

```powershell
pytest -v
```

This verifies:
1. Default settings initialize safely in `DRY_RUN` on `testnet`.
2. Any attempt to use `production`, `mainnet`, or `live` triggers an immediate `SecurityError`.
3. Disabling `DERIBIT_TESTNET` is strictly blocked.
4. Credentials cannot be printed or extracted in plaintext.

---

## Detailed File Breakdown

- **`config/settings.py`**: Houses the `Settings` class built on `pydantic-settings`. Enforces security validations via `@field_validator` and `@model_validator`.
- **`utils/logger.py`**: Standardizes log output across console and rotating file logs (`logs/straddle_engine.log`). Includes regex-based token masking.
- **`main.py`**: Application bootstrap script that validates environment readiness, imports the NautilusTrader Rust core, runs integrity tests, and reports status.
- **`requirements.txt`**: Specifies exact versions for NautilusTrader, Pydantic, and Pytest.
- **`.env.example`**: Safe configuration blueprint for developers.
- **`.gitignore`**: Prevents secrets, virtual environments, bytecode, and logs from entering Git.

---

## Roadmap to Part 2

- **Part 1 (Current)**: Environment setup, dependency installation, security gates, project structure, and import verification.
- **Part 2 (Next)**: Deribit Testnet integration, authentication, instrument subscription, and market data adapter.
- **Part 3**: Straddle pricing, implied volatility estimation, and option chain discovery.
- **Part 4**: NautilusTrader strategy implementation, delta-neutral hedging, and execution.
