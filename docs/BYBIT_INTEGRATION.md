# Bybit Integration Technical Research & Options Capabilities

## Overview
This document delivers an in-depth technical analysis of the **Bybit V5 Integration** in **NautilusTrader 1.231.0**, verifying supported options capabilities, API requirements, and architectural limitations.

---

## 1. Bybit Adapter Architecture in NautilusTrader

NautilusTrader implements a native Bybit V5 integration using:
- **Rust-accelerated WebSockets** (`nautilus_pyo3`) for private and public streaming.
- **HTTP JSON-RPC / REST client** for instrument discovery and account synchronization.
- **Unified Account Protocol** supporting Spot, Linear, Inverse, and Option categories.

```
nautilus_trader.adapters.bybit
├── config.py       # BybitDataClientConfig, BybitExecClientConfig, BybitEnvironment, BybitProductType
├── constants.py    # BYBIT_VENUE, BYBIT_CLIENT_ID
├── data.py         # BybitDataClient (streaming ticks, books, and option greeks)
├── execution.py    # BybitExecutionClient (order routing, order_iv parsing, position reporting)
├── factories.py    # BybitLiveDataClientFactory, BybitLiveExecClientFactory
└── providers.py    # BybitInstrumentProvider (V5 instrument metadata loader)
```

---

## 2. Bybit Environment & Testnet Setup

### 2.1. Environment Enum
NautilusTrader defines `BybitEnvironment` with 3 target environments:
- `BybitEnvironment.TESTNET`: Public testnet sandbox (`https://api-testnet.bybit.com`, `stream-testnet.bybit.com`).
- `BybitEnvironment.DEMO`: Mainnet paper simulated trading.
- `BybitEnvironment.MAINNET`: Live production trading (**strictly prohibited in our configuration**).

### 2.2. Default Testnet Endpoints
- **HTTP REST**: `https://api-testnet.bybit.com`
- **WebSocket Public**: `wss://stream-testnet.bybit.com/v5/public/{product}`
- **WebSocket Private**: `wss://stream-testnet.bybit.com/v5/private`
- **WebSocket Trade**: `wss://stream-testnet.bybit.com/v5/trade`

### 2.3. Automated Credential Sourcing
When `api_key` or `api_secret` are `None`:
- If `environment=BybitEnvironment.TESTNET`: Automatically pulls from `BYBIT_TESTNET_API_KEY` and `BYBIT_TESTNET_API_SECRET`.
- If `environment=BybitEnvironment.MAINNET`: Automatically pulls from `BYBIT_API_KEY` and `BYBIT_API_SECRET`.

---

## 3. Bybit Data Client Configuration

### 3.1. Class Specification: `BybitDataClientConfig`
- **Module**: `nautilus_trader.adapters.bybit.config`
- **Base**: `msgspec.Struct`

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `api_key` | `str \| None` | `None` | Bybit API Key (optional for public feeds) |
| `api_secret` | `str \| None` | `None` | Bybit API Secret |
| `environment` | `BybitEnvironment`| `MAINNET` | Must be explicitly set to `BybitEnvironment.TESTNET` |
| `product_types` | `tuple[BybitProductType, ...]` | All products | `(BybitProductType.OPTION, BybitProductType.LINEAR)` |
| `update_instruments_interval_mins` | `int \| None` | `60` | Frequency for refreshing available option strikes |
| `recv_window_ms` | `int` | `5000` | HTTP request freshness window |
| `bars_timestamp_on_close` | `bool` | `True` | Bar event timestamp alignment |

### 3.2. Verified Code Example: Data Client
```python
from nautilus_trader.adapters.bybit.config import (
    BybitDataClientConfig,
    BybitEnvironment,
    BybitProductType,
)

bybit_data_config = BybitDataClientConfig(
    api_key="your_testnet_key",
    api_secret="your_testnet_secret",
    environment=BybitEnvironment.TESTNET,
    product_types=(
        BybitProductType.OPTION,   # BTC Options chain & Greeks
        BybitProductType.LINEAR,   # BTCUSDT-PERP for delta hedging
    ),
    update_instruments_interval_mins=60,
    recv_window_ms=5000,
)
```

---

## 4. Bybit Execution Client Configuration

### 4.1. Class Specification: `BybitExecClientConfig`
- **Module**: `nautilus_trader.adapters.bybit.config`
- **Base**: `msgspec.Struct`

| Parameter | Type | Default | Critical Constraints |
| :--- | :--- | :--- | :--- |
| `api_key` | `str \| None` | `None` | Required for trading |
| `api_secret` | `str \| None` | `None` | Required for signing requests |
| `environment` | `BybitEnvironment`| `MAINNET` | Must be `BybitEnvironment.TESTNET` |
| `product_types` | `tuple[BybitProductType, ...]`| `(SPOT,)` | **CRITICAL: Must NOT be default!** Defaults to SPOT. For options, must be `(BybitProductType.OPTION, BybitProductType.LINEAR)`. Cannot mix SPOT with derivatives. |
| `use_gtd` | `bool` | `False` | Remaps Good-Till-Date to GTC for local expiration handling |
| `use_ws_execution_fast` | `bool` | `False` | Low-latency private execution stream |
| `use_http_batch_api` | `bool` | `False` | Deprecated in favor of WebSocket execution |
| `ws_trade_timeout_secs` | `float` | `5.0` | Timeout threshold for trade acknowledgments |

### 4.2. Verified Code Example: Execution Client
```python
from nautilus_trader.adapters.bybit.config import (
    BybitExecClientConfig,
    BybitEnvironment,
    BybitProductType,
)

bybit_exec_config = BybitExecClientConfig(
    api_key="your_testnet_key",
    api_secret="your_testnet_secret",
    environment=BybitEnvironment.TESTNET,
    product_types=(
        BybitProductType.OPTION,
        BybitProductType.LINEAR,
    ),
    use_ws_execution_fast=True,
    recv_window_ms=5000,
)
```

---

## 5. Bybit Instrument Provider (`BybitInstrumentProvider`)

`BybitInstrumentProvider` communicates with Bybit V5 `/v5/market/instruments-info` to construct high-performance typed `Instrument` models.

### 5.1. Option Instrument Creation
For `BybitProductType.OPTION`, it parses:
- `raw_symbol`: e.g., `BTC-28NOV26-88500-C`
- `instrument_id`: `BTC-28NOV26-88500-C.BYBIT`
- `underlying`: `BTC`
- `quote_currency`: `USD`
- `settlement_currency`: `BTC`
- `strike_price`: e.g., `88500.00`
- `option_kind`: `OptionKind.CALL` or `OptionKind.PUT`
- `expiration_ns`: Expiration timestamp in nanoseconds UTC

### 5.2. Loading Specific Instruments
```python
from nautilus_trader.model.identifiers import InstrumentId

# Asynchronously load specific option pair
instrument_ids = [
    InstrumentId.from_str("BTC-28NOV26-88500-C.BYBIT"),
    InstrumentId.from_str("BTC-28NOV26-88500-P.BYBIT"),
    InstrumentId.from_str("BTCUSDT-PERP.BYBIT"),
]
await instrument_provider.load_ids_async(instrument_ids)
```

---

## 6. Deep Dive: Bybit Options Capabilities & Limitations

### 6.1. Supported Bybit Options Features ✅
1. **First-Class Product Classification**:
   `BybitProductType.OPTION` is fully integrated into instrument parsing, data clients, and execution clients.
2. **Real-Time Option Greeks Feed**:
   NautilusTrader provides native streaming of exchange-calculated Greeks via `BybitDataClient.subscribe_option_greeks(instrument_id)`.
   - Populates `OptionGreeks`: `delta`, `gamma`, `vega`, `theta`, `rho`, `mark_iv`, `bid_iv`, `ask_iv`, `underlying_price`.
   - Delivered directly to `Strategy.on_option_greeks(self, option_greeks: OptionGreeks)`.
3. **Implied Volatility (IV) Order Placement**:
   In `execution.py`, `_parse_option_params(params, result)` parses:
   - `order_iv`: Submit orders with target Implied Volatility rather than static limit prices.
   - `mmp`: Market Maker Protection flag to protect options quotes against rapid fills.
4. **Unified Margin Account Compatibility**:
   Derivatives execution client automatically sets account type to `AccountType.MARGIN`.
   Allows simultaneous trading of `OPTION` and `LINEAR` (USDT Perpetuals for delta hedging) under one client.

### 6.2. Unsupported Functionality & Architectural Limitations ⚠️

1. **`order_iv` Prohibited in Demo Mode**:
   - In `execution.py`, NautilusTrader raises an exception if `order_iv` is supplied while running in Demo mode:
     *`Option params (order_iv) are not supported in demo mode`*.
   - **Workaround for Testnet**: In Testnet, verify whether your subaccount is Unified Trading Account (UTA). Standard limit orders priced in USD or BTC are universally supported.
2. **Strict Incompatibility with SPOT in Same Execution Client**:
   - Spot trading requires a `CASH` account, whereas Options and Perpetuals require a `MARGIN` account.
   - Attempting to add `BybitProductType.SPOT` alongside `BybitProductType.OPTION` in `BybitExecClientConfig` causes initialization failure.
   - **Implication for Straddle**: Delta hedging MUST be executed using `LINEAR` (e.g. `BTCUSDT-PERP`), NOT Spot.
3. **No Leverage Configuration for Options**:
   - `set_leverage` is explicitly skipped for Options in `execution.py`:
     *`Leverage not supported for options, skipping {symbol}`*.
   - Options margin is governed entirely by Bybit Portfolio Margin rules.
4. **No Position Mode (Hedge Mode) for Options**:
   - Options on Bybit are strictly One-Way. Passing `position_mode` configuration to options instruments is skipped:
     *`Position mode not supported for options, skipping {symbol}`*.
5. **No BBO Order Types for Options**:
   - Bybit Best Bid/Offer queue targeting (`bbo_side_type`, `bbo_level`) is strictly restricted to Linear and Inverse products. Passing BBO parameters to an Option order raises `ValueError`.
6. **No Exchange-Native Multi-Leg Straddle Atomic Order**:
   - Bybit V5 does not support atomic combo orders on options (unlike CME or specialized Eurex protocols).
   - Both legs (Call and Put) must be dispatched as separate limit/market orders.
   - **Engine Requirement**: The strategy must implement client-side orphan leg mitigation (if Leg 1 fills and Leg 2 rejects, immediately neutralize Leg 1).
