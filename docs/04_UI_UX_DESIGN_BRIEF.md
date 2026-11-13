# UI/UX & Operational Interface Design Brief

## Project Information
- **Project**: BTC Options Straddle Trading Engine
- **Assignment**: Mernplus Technologies BTC Options Straddle Project
- **Interface Type**: Institutional Terminal Dashboard (TUI) & Observability Interface

---

## 1. Design Philosophy & Operational Objectives
In algorithmic trading, the user interface must prioritize **clarity, situational awareness, zero ambiguity, and rapid intervention capabilities**. Visual noise is eliminated; critical risk metrics, Greek exposures, and system states are rendered with instant visual priority.

---

## 2. Visual Style & Color Palette

| Token Name | Hex Code | Terminal ANSI / Tag | Operational Meaning |
| :--- | :--- | :--- | :--- |
| **Canvas Dark** | `#121418` | `Black / Dark Gray` | Primary background canvas |
| **Surface Panel**| `#1B1E24` | `Dark Navy / Charcoal` | Card and metric container background |
| **Accent Primary**| `#00E5FF` | `Cyan / Bright Blue` | Headers, timestamps, market index price |
| **Bullish / In-Tolerance** | `#00E676` | `Bright Green` | Positive PnL, delta within safe limits ($|\Delta| \le 0.05$) |
| **Bearish / Drawdown** | `#FF3D71` | `Bright Red` | Negative PnL, stop-loss trigger, order rejects |
| **Warning / Drift** | `#FFC107` | `Amber / Yellow` | Delta drift ($0.05 < |\Delta| \le 0.10$), high margin utilization |
| **Safety Badge** | `#7C4DFF` | `Magenta / Purple` | `[TESTNET]` and `[DRY_RUN]` badges |

---

## 3. Terminal Dashboard Layout Structure

```
+====================================================================================================+
|  BTC OPTIONS STRADDLE ENGINE  |  MODE: [DRY_RUN]  |  NET: [TESTNET ONLY]  |  UPTIME: 04h:22m:15s   |
+====================================================================================================+
|  INDEX PRICE: $88,450.00      |  FORWARD: $88,720.00   |  EXPIRY: 2026-11-28 (15d 04h)             |
+----------------------------------------------------------------------------------------------------+
|  PORTFOLIO & RISK OVERVIEW                                                                         |
|  Equity: 10.4520 BTC ($924,479)     | Margin Usage: 22.4% / 70.0% Max   | Drawdown: -0.84% / 5.0% Max|
|  Unrealized PnL: +0.1240 BTC (+1.2%) | Realized PnL: +0.3280 BTC         | Daily Stop-Loss: ARMED     |
+----------------------------------------------------------------------------------------------------+
|  ACTIVE STRADDLE POSITION (Strike: $88,500)                                                        |
|  Leg 1 (Call): BTC-28NOV26-88500-C  | Qty: +0.10 | Mark: $3,240.00 | Bid/Ask: $3,210 / $3,260      |
|  Leg 2 (Put) : BTC-28NOV26-88500-P  | Qty: +0.10 | Mark: $3,180.00 | Bid/Ask: $3,160 / $3,200      |
|  Total Straddle Premium Paid: $6,420.00          | Current Straddle Value: $6,470.00 (+0.78%)       |
+----------------------------------------------------------------------------------------------------+
|  PORTFOLIO GREEKS & DELTA HEDGE MONITOR                                                            |
|  Net Delta:  +0.018 BTC  [SAFE]     | Gamma: +0.00042        | Vega: +$142.50/vol | Theta: -$88.20/day|
|  Hedge Instrument: BTC-PERPETUAL   | Hedge Position: -0.020 | Hedge Target: 0.000 (Band: +/-0.05) |
+----------------------------------------------------------------------------------------------------+
|  RECENT EXECUTION LOGS (Sanitized & Redacted)                                                      |
|  11:15:02 [INFO] Dual leg orders submitted: Call #ord_812a (Filled), Put #ord_812b (Filled)       |
|  11:16:15 [INFO] Delta drift check: Net Delta = +0.018 BTC <= 0.05 BTC tolerance. No hedge needed.|
|  11:17:00 [INFO] Heartbeat acknowledged by Deribit Testnet. Roundtrip latency: 28ms.              |
+====================================================================================================+
|  HOTKEYS:  [K] Kill Switch / Flatten  |  [H] Rebalance Hedge  |  [P] Pause Engine  |  [Q] Shutdown |
+====================================================================================================+
```

---

## 4. Key Interactive States & UX Behaviors

### 4.1. Empty / Idle State
- Displayed when the bot is initialized but waiting for optimal entry conditions (e.g., waiting for next 08:00 UTC cycle or waiting for spread tightening).
- Visual: Clean panel showing `[STANDBY - SCANNING VOLATILITY SURFACE]`.

### 4.2. In-Flight Execution State
- Displayed during the 5–20 milliseconds when dual-leg orders are routed to the exchange.
- Visual: Amber loading indicator: `[DISPATCHING STRADDLE LEGS...]`.

### 4.3. Delta Rebalancing Alert State
- Triggered when $|\Delta_{\text{net}}| > 0.05\text{ BTC}$.
- Visual: Greek meter flashes Amber, and the log updates: `[HEDGE ACTION] Submitting order: SELL 0.062 BTC-PERPETUAL`.

### 4.4. Risk Stop / Kill Switch State
- Triggered if drawdown touches `5.0%` or user presses `[K]`.
- Visual: Full terminal flashes Red border with bold announcement:
  `[CRITICAL STOP] RISK LIMIT BREACHED - ALL ORDERS CANCELLED - POSITIONS FLATTENED`.

---

## 5. Security & Masking in User Interfaces
- At no point does the dashboard or terminal screen render API keys, secret hashes, or private connection strings.
- All credential fields display `[CONFIGURED - REDACTED]` or `********`.
