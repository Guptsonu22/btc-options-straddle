# Troubleshooting Guide & Operational Runbook

This guide covers diagnostic CLI tools, common error scenarios, root-cause analyses, and verified resolutions for the **BTC Options Straddle Trading Platform**.

---

## 1. Quick Diagnostic Command Matrix

Use these commands to isolate and diagnose issues without placing live orders:

| Diagnostic Goal | Command | What It Checks |
| :--- | :--- | :--- |
| **Configuration & Safety** | `python main.py --config-check` | Validates environment, verifies `BYBIT_TESTNET=True`, tests credential masking. |
| **Bybit WebSocket Handshake**| `python main.py --connectivity-check` | Connects to Bybit Testnet WebSockets, measures latency, disconnects cleanly. |
| **Options Discovery Service**| `python main.py --discover-options` | Queries Bybit REST API, counts active contracts, outputs expiration matrix. |
| **ATM Straddle Selection** | `python main.py --select-straddle` | Ingests live BTC reference price and finds optimal $\min \|K - S\|$ pair. |
| **Live Market Data Stream** | `python main.py --subscribe-quotes` | Streams live `QuoteTick` data for underlying and ATM legs for 12 seconds. |
| **Order Management Engine** | `python main.py --test-order-manager` | Runs dual-leg order state machine and tests orphan leg `CANCEL_AND_CLOSE`. |
| **Level 2 Simulation Suite** | `python main.py --level2-simulations` | Runs all 8 deterministic simulation scenarios (SL, TP, partial fills, rejections). |
| **Level 2 DRY_RUN Execution**| `python main.py --level2-dry-run` | Executes the complete 13-requirement Level 2 lifecycle in simulated memory. |
| **Level 1 DRY_RUN Execution**| `python main.py --level1-dry-run` | Executes the complete 15-step Level 1 lifecycle in simulated memory. |

---

## 2. Common Errors & Resolutions

### 2.1 Security & Configuration Errors

#### Error: `SecurityError: PRODUCTION TRADING IS FORBIDDEN`
- **Cause:** `ENVIRONMENT` is set to `production`, `prod`, `mainnet`, or `live`, or `BYBIT_TESTNET=False`.
- **Resolution:** Set in your `.env` file:
  ```env
  ENVIRONMENT=testnet
  BYBIT_TESTNET=true
  TRADING_MODE=DRY_RUN
  ```

#### Error: `SecurityError: Real credentials were provided while TRADING_MODE is DRY_RUN`
- **Cause:** Active API credentials were supplied when `TRADING_MODE` was set to `DRY_RUN`.
- **Resolution:** For live Testnet execution, set `TRADING_MODE=PAPER` (or omit keys when running in `DRY_RUN`).

---

### 2.2 NautilusTrader Cython & Python Runtime Errors

#### Error: `ValueError: 'bid_price.precision' of 1 was not equal to 'ask_price.precision' of 2`
- **Cause:** NautilusTrader Cython `QuoteTick` requires identical decimal precision between bid and ask prices.
- **Resolution:** Always format prices explicitly with 2 decimal places using `f"{price:.2f}"` (e.g. `Price.from_str("2460.00")`).

#### Error: `DeprecationWarning: There is no current event loop`
- **Cause:** Calling `asyncio.get_event_loop()` in Python 3.10+ when no loop is active.
- **Resolution:** Replaced with `asyncio.get_running_loop()` wrapped in a `try...except RuntimeError` block.

#### Error: `NotImplementedError` on `self.clock.timestamp_ns()`
- **Cause:** Strategy is instantiated outside a running `TradingNode`, leaving `self.clock` as a Cython stub.
- **Resolution:** The codebase incorporates a fallback pattern in `BtcShortStraddleStrategy` and `OrderManager`:
  ```python
  if hasattr(self, "clock") and self.clock is not None:
      try:
          self.clock.timestamp_ns()
          clk = self.clock
      except (NotImplementedError, AttributeError):
          clk = LiveClock()
  ```

---

### 2.3 Exchange & Bybit API Errors

#### Error: Bybit Error `10002: Invalid API key`
- **Cause:** The API key in `.env` does not belong to the Bybit **Testnet** environment (`testnet.bybit.com`). Mainnet keys will fail on Testnet.
- **Resolution:** Generate a key specifically at [https://testnet.bybit.com](https://testnet.bybit.com).

#### Error: Bybit Error `10003: Invalid timestamp or recv_window`
- **Cause:** Local system clock is out of sync with Bybit NTP servers by more than 5,000ms.
- **Resolution:** In Windows PowerShell (as Administrator), synchronize your clock:
  ```powershell
  w32tm /resync
  ```

#### Error: Bybit Error `10006: Position liquidation limit exceeded`
- **Cause:** Maximum position limit exceeded or risk engine rejection.
- **Resolution:** The engine catches this error in `_handle_exit_order_event()` and transitions the exit state to `EXIT_FAILED`. The open position remains tracked for manual operator intervention.

---

### 2.4 Market Data & Liquidity Errors

#### Warning: `Quote for BTC-...-C is STALE (age 6.2s > 5.0s)`
- **Cause:** Bybit Testnet options often have low trading activity, resulting in infrequent ticker updates.
- **Resolution:** Increase `MAX_QUOTE_AGE_SECONDS` in `.env` if testing in quiet Testnet conditions:
  ```env
  MAX_QUOTE_AGE_SECONDS=15.0
  ```

#### Warning: `Crossed orderbook detected: Bid $2,500 >= Ask $2,480`
- **Cause:** Anomalous market data received during orderbook reconstitution.
- **Resolution:** `MarketDataManager` automatically discards crossed quotes to protect the risk engine from false triggers.

---

## 3. Log Inspection & Secrets Protection

Logs are written to both standard output and `logs/straddle_engine.log`.

### 3.1 Verifying Credential Masking
The custom `SensitiveDataFilter` scrubs all API keys and tokens:
```text
2026-11-13 15:13:46 [INFO] [btc_straddle:main.py:132]   BYBIT_API_KEY: [REDACTED]
2026-11-13 15:13:46 [INFO] [btc_straddle:main.py:132]   BYBIT_API_SECRET: None
```

### 3.2 Tail Live Logs
In PowerShell:
```powershell
Get-Content -Path logs/straddle_engine.log -Tail 50 -Wait
```
