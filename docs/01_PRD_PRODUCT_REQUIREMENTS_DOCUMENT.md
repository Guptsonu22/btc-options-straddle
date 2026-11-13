# Product Requirements Document (PRD)

## Project Information
- **Project Name**: BTC Options Straddle Trading Engine
- **Assignment**: Mernplus Technologies BTC Options Straddle Project
- **Target Exchange**: Deribit Testnet (`https://test.deribit.com`)
- **Core Technology**: NautilusTrader (Rust/Python), Pydantic, WebSockets/JSON-RPC

---

## 1. Executive Summary & One-Line Vision
An institutional-grade, event-driven algorithmic trading engine that executes automated, delta-neutral Bitcoin (BTC) options straddle strategies with dynamic delta hedging, rigorous pre-trade risk gates, and sub-millisecond execution control on Deribit Testnet.

---

## 2. Target Users & Stakeholders
1. **Quantitative Options Traders**: Requiring automated volatility surface analysis, ATM strike identification, and simultaneous multi-leg order execution.
2. **Risk Officers & Portfolio Managers**: Requiring strict margin utilization bounds, automated stop-out drawdown triggers, and real-time Greek exposure monitoring ($\Delta$, $\Gamma$, $\mathcal{V}$, $\Theta$).
3. **Execution Engineers**: Requiring low-latency order routing, zero execution leg-risk, automated fill reconciliation, and complete dry-run / testnet safety isolation.

---

## 3. Problem Statement & Market Friction
1. **Execution Leg Risk**: Entering a Straddle manually involves purchasing a Call and a Put sequentially. Rapid market movement between leg fills creates adverse slippage and unwanted directional bias.
2. **Dynamic Delta Drift**: Options delta changes continuously as BTC spot price fluctuates and time decays ($\Theta$). Unhedged straddles rapidly become directional bets unless dynamically rebalanced with the underlying or perpetual futures.
3. **Operational & Security Hazards**: Many algorithmic trading scripts expose API keys in plaintext, lack environment gates, or fail to gracefully handle WebSocket disconnections during volatile regimes.
4. **Lack of Institutional Architecture**: Prototype scripts lack backtest-to-live state parity, event-driven order book processing, and structured audit logs.

---

## 4. Core Features

### 4.1. Automated Instrument & Volatility Surface Discovery
- Connects to Deribit Testnet via WebSocket feed.
- Ingests the active BTC options chain across selectable maturities (e.g., Daily, Weekly, Monthly).
- Identifies the At-The-Money (ATM) strike price based on the live Deribit BTC Index / Perpetual price.

### 4.2. Synchronized Dual-Leg Straddle Execution
- Simulates or dispatches simultaneous buy/sell orders for the ATM Call and ATM Put.
- Configurable strategy regimes:
  - **Long Straddle**: Simultaneous Long Call + Long Put (long volatility, profit from large moves).
  - **Short Straddle**: Simultaneous Short Call + Short Put (short volatility, profit from range-bound price action & theta decay).
- Fills synchronized with strict slippage and time-in-force parameters (IOC / FOK or resting Post-Only limit orders).

### 4.3. Real-Time Greeks & Pricing Engine
- Computes Black-76 / Bjerksund-Stensland option Greeks in real-time:
  - **Delta ($\Delta$)**: Directional sensitivity.
  - **Gamma ($\Gamma$)**: Rate of change of Delta.
  - **Vega ($\mathcal{V}$)**: Volatility sensitivity per 1% move in IV.
  - **Theta ($\Theta$)**: Daily time decay.
- Tracks Net Portfolio Delta: $\Delta_{net} = \Delta_{call} + \Delta_{put} + \Delta_{hedge}$.

### 4.4. Dynamic Delta-Neutral Hedging Loop
- Automatically rebalances net delta using BTC-PERPETUAL or spot when $\Delta_{net}$ breaches configured drift tolerance bands (e.g., $|\Delta_{net}| > 0.05\text{ BTC}$).
- Minimizes transaction costs by avoiding over-hedging via deadband thresholds.

### 4.5. Multi-Layer Pre-Trade Risk Engine & Kill Switch
- **Hard Max Drawdown Stop**: Automatic strategy halt and position liquidation if portfolio equity drops below `MAX_DRAWDOWN_PCT` (default 5.0%).
- **Margin Limit Guardian**: Prevents order placement if maintenance margin ratio exceeds 70%.
- **Emergency Kill Switch**: Immediate cancel-all and optional position flattening command.

### 4.6. Defense-in-Depth Security
- **Enforced Testnet**: Rejects connection if production environment is detected.
- **DRY_RUN Mode**: Complete simulated execution mode for zero-risk algorithmic validation.
- **Secret Redaction**: Credentials stored as opaque `SecretStr` and automatically filtered out of all log sinks.

---

## 5. User Personas & User Stories

| Persona | Story | Acceptance Criteria |
| :--- | :--- | :--- |
| **Quant Trader** | As a trader, I want the bot to automatically select the ATM strike at 08:00 UTC daily for the next expiration. | Engine queries Deribit index, finds closest strike, and verifies liquidity depth. |
| **Execution Bot** | As an engine, I want to route Call and Put orders concurrently within 10ms. | Dual orders submitted simultaneously; if one rejects, the counter-leg is aborted. |
| **Risk Manager** | As a risk manager, I want the engine to maintain net delta within $\pm 0.05$ BTC. | Perpetual order dispatched automatically whenever delta exceeds threshold. |
| **DevOps Engineer** | As an engineer, I want the system to abort if someone sets `ENVIRONMENT=production`. | System raises `SecurityError` and refuses to initialize. |

---

## 6. Success Metrics & Performance KPIs

1. **Order Synchronization Latency**: Time differential between dual legs $< 15\text{ ms}$.
2. **Delta Neutrality Compliance**: Net portfolio delta held within tolerance $\ge 98\%$ of trading session duration.
3. **Execution Slippage**: Realized fill vs mid-quote slippage $< 0.15\%$ of underlying price.
4. **Zero Security Exposure**: 0 plaintext API keys logged or committed to Git.
5. **System Uptime & Resiliency**: Automatic WebSocket reconnection with zero message loss or duplicate order dispatch.

---

## 7. Scope Boundaries

### In-Scope (MVP & Part 1–4):
- Project setup, security guardrails, and virtual environment (Part 1 - Complete).
- Deribit Testnet market data and order routing adapter (Part 2).
- ATM discovery, Black-76 Greeks calculation, and pricing model (Part 3).
- NautilusTrader Strategy class with dual-leg entry and delta-hedge manager (Part 4).
- Comprehensive test suite and dry-run execution proof.

### Out-of-Scope (Future Enhancements):
- Multi-exchange cross-arbitrage (e.g., Deribit vs Bybit/OKX).
- Exotic option structures (Butterflies, Condors, Variance Swaps).
- Mainnet real-money trading (strictly prohibited by assignment policy).
- GUI desktop application (CLI and structured console dashboard utilized).
