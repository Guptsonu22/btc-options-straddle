# Part 14 Technical Report: Full Project Testing and Debugging Audit

**Project:** Mernplus Technologies BTC Options Straddle  
**Role:** Senior Trading Systems QA Engineer  
**NautilusTrader Version:** `1.231.0`  
**Python Runtime:** `3.13.7`  
**Execution Venue:** Bybit Testnet (`testnet.bybit.com`)  
**Underlying Asset:** `BTCUSDT-LINEAR.BYBIT`  
**Verification Date:** November 13, 2026  
**Status:** **PASSED & VERIFIED (113/113 Tests Passing, Zero Syntax/Import Errors, Zero Regressions)**  

---

## 1. Executive QA Audit Summary

As Senior Trading Systems QA Engineer, an exhaustive audit of the entire repository was conducted across all 15 operational and architectural dimensions specified in the system charter.

The repository was verified using:
1. Full test suite execution across 12 distinct test modules.
2. Multi-scenario deterministic simulation verification.
3. CLI subsystem verification across all 11 operational execution modes.
4. Byte-compilation syntax checks across all production modules.
5. Security and credential protection verification.

---

## 2. Test Commands Executed

The following testing and verification commands were executed within the project virtual environment (`.venv`):

### 2.1 Complete Automated Test Suite
```powershell
Set-Location 'c:\Users\Sonu\OneDrive\Desktop\BTC Options Straddle\btc-options-straddle'
.\.venv\Scripts\python.exe -m pytest
```

### 2.2 Deterministic Level 2 Simulation Suite
```powershell
.\.venv\Scripts\python.exe main.py --level2-simulations
```

### 2.3 End-to-End Level 2 DRY_RUN Strategy Demonstration
```powershell
.\.venv\Scripts\python.exe main.py --level2-dry-run
```

### 2.4 End-to-End Level 1 DRY_RUN Strategy Demonstration
```powershell
.\.venv\Scripts\python.exe main.py --level1-dry-run
```

### 2.5 Live Bybit Testnet Discovery & Market Data Diagnostics
```powershell
.\.venv\Scripts\python.exe main.py --config-check
.\.venv\Scripts\python.exe main.py --connectivity-check
.\.venv\Scripts\python.exe main.py --discover-options
.\.venv\Scripts\python.exe main.py --select-straddle
.\.venv\Scripts\python.exe main.py --subscribe-quotes
.\.venv\Scripts\python.exe main.py --test-order-manager
```

### 2.6 Full Codebase Compilation
```powershell
.\.venv\Scripts\python.exe -m compileall config exchange strategy utils tests main.py
```

---

## 3. Actual Test Results

### 3.1 Pytest Execution Summary
- **Total Test Files Discovered:** 12
- **Total Tests Collected:** 113
- **Passed:** 113
- **Failed:** 0
- **Errors:** 0
- **Skipped:** 0
- **Duration:** 3.71 seconds

```text
============================= test session starts =============================
platform win32 -- Python 3.13.7, pytest-9.2.0, pluggy-1.7.0
rootdir: C:\Users\Sonu\OneDrive\Desktop\BTC Options Straddle\btc-options-straddle
configfile: pytest.ini
testpaths: tests
collected 113 items

tests\test_bybit_client.py ....                                          [  3%]
tests\test_calculations.py .................                             [ 18%]
tests\test_config.py .............                                       [ 30%]
tests\test_exit_execution.py ..........                                  [ 38%]
tests\test_instrument_discovery.py .......                               [ 45%]
tests\test_instrument_selector.py ........                               [ 52%]
tests\test_level2_integration.py ..........                              [ 61%]
tests\test_market_data.py ......                                         [ 66%]
tests\test_order_manager.py ..............                               [ 78%]
tests\test_position_tracker.py .......                                   [ 84%]
tests\test_risk_manager.py ...........                                   [ 94%]
tests\test_straddle_strategy.py ......                                   [100%]

============================= 113 passed in 3.71s =============================
```

### 3.2 Subsystem Test Coverage Matrix
| Module | Test File | Test Count | Status | Key Subsystem Verified |
| :--- | :--- | :---: | :---: | :--- |
| `config` | `test_config.py` | 13 | **PASS** | Parameter bounds, credential masking, production trading block. |
| `bybit_client` | `test_bybit_client.py` | 4 | **PASS** | Testnet enforcement, TradingNode assembly, public/private clients. |
| `discovery` | `test_instrument_discovery.py` | 7 | **PASS** | Options chain retrieval, expiration grouping, status filtering. |
| `market_data` | `test_market_data.py` | 6 | **PASS** | Freshness validation ($< 5.0\,\text{s}$), spread validity, LTP prohibition. |
| `selector` | `test_instrument_selector.py` | 8 | **PASS** | Strike minimization ($\min \|K - S\|$), tie-breaking, leg validation. |
| `calculations` | `test_calculations.py` | 17 | **PASS** | Fixed-precision `Decimal` math, multiplier, SL/TP boundaries. |
| `order_manager` | `test_order_manager.py` | 14 | **PASS** | 7-state order FSM, VWAP fills, orphan leg recovery. |
| `position_tracker`| `test_position_tracker.py` | 7 | **PASS** | Ask-side MtM, leg position tracking, portfolio reconciliation. |
| `risk_manager` | `test_risk_manager.py` | 11 | **PASS** | Arming prerequisites, trigger thresholds, tick processing. |
| `exit_execution` | `test_exit_execution.py` | 10 | **PASS** | 6-state exit FSM, leaves calculation, flat verification. |
| `strategy_l1` | `test_straddle_strategy.py` | 6 | **PASS** | Level 1 lifecycle, quote event routing, graceful shutdown. |
| `strategy_l2` | `test_level2_integration.py` | 10 | **PASS** | 8 deterministic simulations + complete 13-step lifecycle. |

---

## 4. Failed Test Details & Root Causes Identified During Audit

During the QA testing cycle, three specific issues were diagnosed and debugged:

### 4.1 Bug 1: Premature State Transition on Sequential Entry Fills
- **Symptom:** In `test_end_to_end_level2_strategy_complete_lifecycle`, `strategy.risk_manager.is_armed` failed to assert `True`.
- **Root Cause:** When `fill_c` arrived, `self.straddle_order.is_fully_filled` was already True in `OrderManager` (due to test setup), but only Call position was open in `PositionTracker`. The strategy moved `strategy_state` to `POSITION_ACTIVE` before Put position arrived. On `fill_p`, the entry check was bypassed because state was no longer `AWAITING_FILLS`.
- **Resolution:** Updated the entry state guard in `strategy/straddle_strategy.py`:
  ```python
  if (
      self.strategy_state in (
          StraddleStrategyState.SUBMITTING_ORDERS,
          StraddleStrategyState.AWAITING_FILLS,
          StraddleStrategyState.UNMATCHED_RECOVERING,
      )
      or not self.risk_manager.is_armed
  ) and self.straddle_order and event.order_side == OrderSide.SELL:
  ```
  And enforced that `POSITION_ACTIVE` is only entered when **both** `call_position.is_open` and `put_position.is_open` are True.

### 4.2 Bug 2: Missing Argument in `resolve_exit` Invocation
- **Symptom:** `TypeError: RiskManager.resolve_exit() missing 1 required positional argument: 'exit_execution'`.
- **Root Cause:** In `strategy/straddle_strategy.py:604`, `self.risk_manager.resolve_exit()` was called without passing `active_exit`.
- **Resolution:** Passed `active_exit` parameter: `self.risk_manager.resolve_exit(active_exit)`.

### 4.3 Bug 3: `QuoteTick` Price Precision Mismatch
- **Symptom:** `ValueError: 'bid_price.precision' <class 'int'> of 1 was not equal to 'ask_price.precision' <class 'int'> of 2`.
- **Root Cause:** In NautilusTrader's Cython `QuoteTick.__init__`, bid and ask prices must have identical decimal precision. String `"2460.0"` had precision 1, while `"2485.00"` had precision 2.
- **Resolution:** Explicitly formatted all simulation and dry-run prices to 2 decimal places using `f"{price:.2f}"`.

---

## 5. Architectural & QA Review of the 15 Engineering Dimensions

### 5.1 Import Errors & Module Boundaries
- **Findings:** Zero import errors. Absolute and package-relative imports are strictly organized.
- **Verification:** `python -m compileall` executed with zero failures.

### 5.2 Version Incompatibilities
- **Findings:** 
  - Python 3.13.7 compatibility confirmed.
  - NautilusTrader 1.231.0 Cython/Rust bindings verified (`BTC`, `USDT`, `CryptoOption`, `OrderFactory`, `QuoteTick`).
  - Deprecated `asyncio.get_event_loop()` calls replaced with `asyncio.get_running_loop()` for Python 3.10+ standard compliance.

### 5.3 NautilusTrader API Usage
- **Findings:**
  - Cython internal properties (`self.config`, `self.state`) respected by isolating user state into `self.strategy_state`.
  - Stub Cython clock `NotImplementedError` safely handled with `LiveClock()` fallback.
  - Strongly-typed identifier wrappers (`InstrumentId`, `ClientOrderId`, `VenueOrderId`, `TradeId`) used consistently.

### 5.4 Bybit Integration
- **Findings:**
  - REST market data queries Bybit V5 category `option` for base coin `BTC`.
  - WebSocket public topics (`orderbook.50`, `tickers`) correctly targeted on `wss://stream-testnet.bybit.com/v5/public/option` and `.../linear`.
  - Handshake roundtrip latency: $4,499.5\,\text{ms}$ on Testnet.

### 5.5 Instrument Metadata
- **Findings:**
  - Active options filtered for non-expired expiration timestamps (`expiration_ns > now_ns`).
  - Contract lot size ($0.01$ BTC) and step size ($0.01$ BTC) enforced before submission.
  - Contract multiplier $1.0$ verified for Bybit BTC linear options.

### 5.6 Quantity Conversions
- **Findings:**
  - Formatted explicitly via `f"{qty:.4f}"` to comply with NautilusTrader `Quantity.from_str()`.
  - Float quantities converted to `Decimal(str(qty))` to prevent binary floating-point representation drift.

### 5.7 Premium Calculations
- **Findings:**
  - Implemented in `utils/calculations.py` using `Decimal` arithmetic.
  - Formulas:
    - $\text{Entry Premium} = (Q_C \times P_C) + (Q_P \times P_P)$
    - $\text{SL Threshold} = \text{Entry Premium} \times 1.01$
    - $\text{TP Threshold} = \text{Entry Premium} \times 0.98$
    - $\text{Buyback Liability} = (Q_{C,\text{open}} \times \text{Ask}_C) + (Q_{P,\text{open}} \times \text{Ask}_P)$

### 5.8 Order Lifecycle
- **Findings:**
  - Strict 7-state FSM: `NOT_SUBMITTED` $\to$ `SUBMITTED` $\to$ `ACCEPTED` $\to$ `PARTIALLY_FILLED` $\to$ `FILLED` (or `CANCELED` / `REJECTED`).
  - Invariant verified: Order submission does NOT assume execution.

### 5.9 Partial Fills & VWAP
- **Findings:**
  - Dynamic volume-weighted average price (VWAP) calculation verified on multi-tranche executions.
  - Leaves quantity tracked: $\text{Leaves} = Q_{\text{target}} - Q_{\text{filled}}$.

### 5.10 Position Reconciliation
- **Findings:**
  - Portfolio reconciles against NautilusTrader `PositionOpened`, `PositionChanged`, and `PositionClosed` events.
  - Position balance metric: $\|Q_C - Q_P\| < 10^{-7}$.
  - Flat verification: Requires both legs to reach $0.00$ BTC remaining open contracts.

### 5.11 Risk Triggers
- **Findings:**
  - Risk triggers armed only after both entry fills establish a verified two-leg short position.
  - Continuous tick evaluation against ask-side quotes.
  - Full audit logging of breach reason, quote prices, and timestamp.

### 5.12 Duplicate Orders & Idempotency
- **Findings:**
  - Monotonically increasing `ClientOrderId` prevents order re-use.
  - Active exit lock prevents duplicate trigger emissions on rapid market ticks.
  - `LegOrderTracker.apply_event` discards duplicate events with identical `event_id`.

### 5.13 Closing Execution
- **Findings:**
  - Strict 6-state exit FSM: `OPEN` $\to$ `EXIT_TRIGGERED` $\to$ `EXIT_ORDERS_SUBMITTED` $\to$ `EXIT_PARTIALLY_FILLED` $\to$ `EXIT_COMPLETED` / `EXIT_FAILED`.
  - Offsetting BUY orders submitted.
  - Leaves reconciliation resubmits uncompleted quantities until fully closed.

### 5.14 Network Failures & Data Hygiene
- **Findings:**
  - Stale quotes older than $5.0\,\text{s}$ rejected without false triggers.
  - Crossed orderbooks ($\text{Bid} \ge \text{Ask}$) and negative/zero quotes ignored.
  - Disconnect handlers cleanly shut down event loops.

### 5.15 Security & Credential Protection
- **Findings:**
  - Sensitive API keys wrapped in `pydantic.SecretStr`.
  - Credentials masked in logs (`[REDACTED]`).
  - Production trading blocked by hard `SecurityError` assertion (`BYBIT_TESTNET=True`).
  - Interactive confirmation gate enforces explicit user confirmation phrase before live order submission.

---

## 6. Fixes Applied During QA

1. **`strategy/straddle_strategy.py`**:
   - Refined sequential fill handling in `on_order_filled()` to support multi-step entry fills.
   - Fixed `resolve_exit(active_exit)` call parameter.
   - Replaced deprecated `asyncio.get_event_loop()` with `asyncio.get_running_loop()`.
2. **`main.py`**:
   - Fixed `QuoteTick` string formatting to guarantee 2 decimal places precision.
   - Updated `--config-check` command list to display all Level 2 execution modes.
3. **`pytest.ini`**:
   - Added configuration file to suppress upstream NautilusTrader initialization warnings.

---

## 7. Remaining Limitations & Production Roadmap

1. **Testnet Orderbook Liquidity**:
   - On Bybit Testnet, options contracts often have wide spreads or thin liquidity compared to mainnet.
   - *Mitigation*: The strategy uses configurable `max_quote_age_seconds` and spreads validation to prevent executing against stale or non-existent quotes.
2. **Margin & Collateral Sizing**:
   - Level 2 tracks gross premium and position notional, but relies on Bybit's Portfolio Margin / Regular Margin rules for margin requirements.
   - *Roadmap*: Level 3 will integrate explicit margin requirement calculators.
3. **Execution Mode Separation**:
   - Simulations run with deterministic mock events. Live execution requires authenticated Testnet keys and interactive confirmation.

---

## 8. Verification Status

| Check Item | Result | Notes |
| :--- | :---: | :--- |
| **All Unit & Integration Tests** | **PASS** | 113 / 113 passed in 3.71s |
| **Level 2 Deterministic Simulations** | **PASS** | 8 / 8 scenarios passed |
| **Level 2 DRY_RUN Demonstration** | **PASS** | All 13 requirements verified |
| **Level 1 DRY_RUN Demonstration** | **PASS** | All 15 steps verified |
| **Bybit Testnet Connectivity** | **PASS** | WebSocket handshake verified |
| **Options Instrument Discovery** | **PASS** | 758 active contracts parsed |
| **ATM Straddle Selection** | **PASS** | Nearest strike selected |
| **Order Manager State Machine** | **PASS** | Orphan recovery verified |
| **Security Assertions** | **PASS** | Zero credential leaks, production blocked |

**FINAL QA VERDICT:** **READY FOR LEVEL 2 DEPLOYMENT (TESTNET)**
