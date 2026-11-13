# Strategy Flow & Lifecycle: BTC Short Straddle Trading System

This document outlines the theoretical mechanics, mathematical payoff structures, and complete algorithmic lifecycle of the **BTC Short Straddle Strategy**.

---

## 1. Options Straddle Theory & Mechanics

### 1.1 What is a Short Straddle?
A **Short Straddle** is a market-neutral options strategy created by simultaneously selling a Call option and a Put option with the **identical strike price ($K$)** and the **identical expiration date ($T$)**:

$$\text{Strategy Composition} = \text{SELL 1 Call}(K, T) + \text{SELL 1 Put}(K, T)$$

```
Payoff ($)
    ^
    |          /\ (Maximum Profit = Combined Premium Collected)
    |         /  \
----+--------/----\--------> Underlying BTC Spot Price ($)
    |       /  |   \
    |      /   K    \
    |     /          \
    |    / (Loss)     \ (Loss)
    v
```

### 1.2 Greeks & Risk Profile at Initiation
- **Delta ($\Delta \approx 0$)**: At-The-Money Call delta is $\approx +0.50$; Short Call delta is $\approx -0.50$. At-The-Money Put delta is $\approx -0.50$; Short Put delta is $\approx +0.50$. Net position delta is approximately **$0.00$ (Delta Neutral)**.
- **Theta ($\Theta > 0$)**: Positive time decay. Every day that passes without significant underlying price movement increases the profit of the position as option premiums decay toward zero.
- **Vega ($\nu < 0$)**: Negative volatility exposure. The strategy profits when implied volatility (IV) contracts and suffers when IV expands.
- **Gamma ($\Gamma < 0$)**: Negative gamma exposure. Large sudden moves in either direction cause the position delta to drift rapidly away from neutral, creating expanding liability.

---

## 2. End-to-End Strategy Sequence Diagram

```mermaid
sequenceDiagram
    autonumber
    actor Trader as Trader / CLI
    participant Strategy as BtcShortStraddleStrategy
    participant Discovery as BtcOptionsDiscoveryService
    participant Selector as InstrumentSelector
    participant Venue as Bybit Testnet Venue
    participant MarketData as MarketDataManager
    participant OrderMgr as OrderManager
    participant PosTracker as PositionTracker
    participant RiskMgr as RiskManager

    Note over Trader,Strategy: Phase 1: Initialization & Discovery
    Trader->>Strategy: python main.py --level2-dry-run (on_start)
    Strategy->>Discovery: discover_btc_options()
    Discovery->>Venue: GET /v5/market/instruments-info?category=option
    Venue-->>Discovery: 836 raw option contracts
    Discovery-->>Strategy: DiscoveredOptionsChain (758 active)

    Note over Strategy,Selector: Phase 2: ATM Strike Selection
    Strategy->>MarketData: on_quote_tick(BTCUSDT-LINEAR.BYBIT)
    MarketData-->>Strategy: BTC Reference Spot = $70,000.00
    Strategy->>Selector: select_straddle_pair(chain, spot=70000)
    Selector-->>Strategy: SelectedStraddle (Strike=$70,000, 2 DTE, Call & Put matched)

    Note over Strategy,OrderMgr: Phase 3: Dual SELL Entry Execution
    Strategy->>OrderMgr: create_straddle_sell_orders(Call, Put, qty=0.05 BTC)
    OrderMgr->>Venue: SubmitOrder(SELL Call Limit @ Bid)
    OrderMgr->>Venue: SubmitOrder(SELL Put Limit @ Bid)
    Venue-->>OrderMgr: OrderAccepted (Call: VENUE-C01, Put: VENUE-P01)
    Venue-->>OrderMgr: OrderFilled (Call: 0.05 BTC @ $2,480.00)
    Venue-->>OrderMgr: OrderFilled (Put: 0.05 BTC @ $2,480.00)
    OrderMgr-->>Strategy: StraddleOrder (is_fully_filled = True)

    Note over Strategy,RiskMgr: Phase 4: Position Accounting & Thresholds
    Strategy->>PosTracker: apply_fill(Call, SELL, 0.05, 2480.00)
    Strategy->>PosTracker: apply_fill(Put, SELL, 0.05, 2480.00)
    PosTracker-->>Strategy: StraddlePosition (Balanced: -0.05 Call, -0.05 Put)
    Strategy->>RiskMgr: arm_from_straddle_position(StraddlePosition)
    Note right of RiskMgr: Entry Premium = $248.00<br/>SL Threshold (+1%) = $250.48<br/>TP Target (-2%) = $243.04

    Note over Strategy,RiskMgr: Phase 5: Continuous Ask-Side Risk Monitoring
    loop On Every Valid QuoteTick
        Venue->>MarketData: QuoteTick(Call Ask, Put Ask)
        MarketData->>RiskMgr: on_quote_tick(tick)
        RiskMgr->>RiskMgr: Buyback = (Q_c * Ask_c) + (Q_p * Ask_p)
        alt Buyback >= SL_THRESHOLD (Stop-Loss)
            RiskMgr->>RiskMgr: Trigger STOP_LOSS
        else Buyback <= TP_THRESHOLD (Take-Profit)
            RiskMgr->>RiskMgr: Trigger TAKE_PROFIT
        end
    end

    Note over RiskMgr,OrderMgr: Phase 6: Automated Liquidation & Reconciliation
    RiskMgr->>OrderMgr: initiate_straddle_exit(trigger=STOP_LOSS)
    OrderMgr->>Venue: SubmitOrder(BUY Call Market/Aggressive Limit, 0.05 BTC)
    OrderMgr->>Venue: SubmitOrder(BUY Put Market/Aggressive Limit, 0.05 BTC)
    Venue-->>OrderMgr: OrderFilled (Call Closing Buy)
    OrderMgr->>PosTracker: apply_fill(Call, BUY, 0.05)
    Venue-->>OrderMgr: OrderFilled (Put Closing Buy)
    OrderMgr->>PosTracker: apply_fill(Put, BUY, 0.05)
    PosTracker-->>Strategy: is_flat == True (0.00 BTC remaining)
    Strategy->>RiskMgr: resolve_exit(active_exit)
    Strategy->>Strategy: State -> POSITION_CLOSED
    Strategy-->>Trader: Strategy Complete (Verified Flat)
```

---

## 3. Detailed Step-by-Step Lifecycle Execution

### Step 1: Initialization & Environment Validation
- Instantiates `TradingNode`, Cython `LiveClock`, and `OrderFactory`.
- Validates configuration parameters.
- Asserts `BYBIT_TESTNET=True`.

### Step 2: Options Chain Discovery & Filtering
- Queries Bybit REST API for active options.
- Discards expired contracts (`expiration_ns <= now_ns`).
- Groups contracts by expiration date and validates that lot sizes meet the $0.01$ BTC minimum.

### Step 3: Underlying Spot Ingestion & ATM Selection
- Ingests real-time underlying reference price from `BTCUSDT-LINEAR.BYBIT`.
- Solves $\min |K - S|$ across available strikes for the target expiration.
- Validates that both the Call and Put exist, have identical strikes and expiries, and are actively trading.

### Step 4: Quote Freshness & Spread Verification
- Subscribes to real-time WebSocket feeds for selected Call and Put legs.
- Validates quote freshness ($age < 5.0\,\text{s}$).
- Verifies positive bid and ask prices ($\text{Bid} > 0, \text{Ask} > 0$) and non-inverted orderbooks ($\text{Bid} < \text{Ask}$).

### Step 5: Dual Limit SELL Order Generation
- Constructs dual SELL Limit orders at current Bid prices using `OrderFactory`.
- Assigns monotonic `ClientOrderId` strings.
- Submits orders via `OrderManager`. Enforces invariant: **Order submission $\ne$ execution.**

### Step 6: Fill Processing & VWAP Calculation
- Awaits venue execution confirmations.
- Updates cumulative fill quantities and volume-weighted average prices:
  $$\text{VWAP} = \frac{\sum (\text{Fill Quantity}_i \times \text{Fill Price}_i)}{\sum \text{Fill Quantity}_i}$$
- Tracks individual commissions and trade IDs.

### Step 7: Risk Manager Arming & Threshold Definition
- Requires both Call and Put positions to be confirmed open in `PositionTracker`.
- Calculates gross entry premium: $\text{Entry} = (Q_C \times P_C) + (Q_P \times P_P)$.
- Establishes exact mathematical thresholds:
  - Stop-Loss: $\text{SL\_THRESHOLD} = \text{Entry} \times 1.01$ (+1.0% liability increase)
  - Take-Profit: $\text{TP\_THRESHOLD} = \text{Entry} \times 0.98$ (-2.0% liability decrease)

### Step 8: Continuous Mark-to-Market Monitoring
- Streams incoming ask quotes for both options legs.
- Evaluates live buyback cost:
  $$\text{CURRENT\_BUYBACK\_VALUE} = (Q_{C,\text{open}} \times \text{Ask}_C) + (Q_{P,\text{open}} \times \text{Ask}_P)$$
- **Strict Rule**: Mark-to-market strictly uses the **ASK price** (the price to buy back short options). LTP and mid-prices are strictly prohibited.

### Step 9: Risk Breach & Automated Liquidation
- If $\text{Buyback} \ge \text{SL\_THRESHOLD}$ or $\text{Buyback} \le \text{TP\_THRESHOLD}$:
  - Raises `RiskTriggerEvent`.
  - Sets exit state to `EXIT_TRIGGERED`.
  - Locks against duplicate triggers.
  - Queries `PositionTracker` for actual open short quantities.
  - Generates offsetting BUY orders sized exactly to open positions.

### Step 10: Leaves Reconciliation & Flat Verification
- As fills arrive, computes remaining leaves: $\text{Leaves} = Q_{\text{target}} - Q_{\text{filled}}$.
- Resubmits residual orders if a leg partially fills.
- When all closing fills arrive, queries `PositionTracker.is_flat`.
- Transitions strategy state to `POSITION_CLOSED`.
- Logs final realized PnL and executes clean shutdown.
