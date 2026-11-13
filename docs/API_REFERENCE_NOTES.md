# NautilusTrader & Bybit API Reference Notes

## Overview
Verified developer cheat-sheet and import reference catalog for **NautilusTrader 1.231.0**, validated on Windows 11 with Python 3.13.7.

---

## 1. Verified Import Catalog

| Class / Enum / Symbol | Verified Import Path | Description |
| :--- | :--- | :--- |
| `TradingNode` | `from nautilus_trader.live.node import TradingNode` | Main execution process orchestrator |
| `TradingNodeConfig` | `from nautilus_trader.config import TradingNodeConfig` | Node configuration struct |
| `Trader` | `from nautilus_trader.trading.trader import Trader` | Strategy supervisor (`node.trader`) |
| `BybitDataClientConfig` | `from nautilus_trader.adapters.bybit.config import BybitDataClientConfig` | Bybit market data settings |
| `BybitExecClientConfig` | `from nautilus_trader.adapters.bybit.config import BybitExecClientConfig` | Bybit order execution settings |
| `BybitEnvironment` | `from nautilus_trader.adapters.bybit.config import BybitEnvironment` | `TESTNET`, `DEMO`, `MAINNET` |
| `BybitProductType` | `from nautilus_trader.adapters.bybit.config import BybitProductType` | `OPTION`, `LINEAR`, `SPOT`, `INVERSE` |
| `BybitLiveDataClientFactory` | `from nautilus_trader.adapters.bybit.factories import BybitLiveDataClientFactory` | Data client builder factory |
| `BybitLiveExecClientFactory` | `from nautilus_trader.adapters.bybit.factories import BybitLiveExecClientFactory` | Exec client builder factory |
| `BybitInstrumentProvider` | `from nautilus_trader.adapters.bybit.providers import BybitInstrumentProvider` | V5 instrument catalog loader |
| `BYBIT_VENUE` | `from nautilus_trader.adapters.bybit.constants import BYBIT_VENUE` | `Venue("BYBIT")` constant |
| `InstrumentId` | `from nautilus_trader.model.identifiers import InstrumentId` | Full instrument identifier (`<SYM>.<VENUE>`) |
| `Symbol` | `from nautilus_trader.model.identifiers import Symbol` | Instrument symbol component |
| `Venue` | `from nautilus_trader.model.identifiers import Venue` | Venue component |
| `OptionContract` | `from nautilus_trader.model.instruments import OptionContract` | Typed options contract model |
| `Price` | `from nautilus_trader.model.objects import Price` | Nanosecond/fixed-precision price |
| `Quantity` | `from nautilus_trader.model.objects import Quantity` | Nanosecond/fixed-precision quantity |
| `OrderSide` | `from nautilus_trader.model.enums import OrderSide` | `BUY`, `SELL` |
| `OrderType` | `from nautilus_trader.model.enums import OrderType` | `LIMIT`, `MARKET`, `STOP_MARKET` |
| `TimeInForce` | `from nautilus_trader.model.enums import TimeInForce` | `GTC`, `IOC`, `FOK` |
| `Strategy` | `from nautilus_trader.trading.strategy import Strategy` | Base class for trading logic |
| `StrategyConfig` | `from nautilus_trader.config import StrategyConfig` | Base config for strategy subclasses |
| `OrderFactory` | `from nautilus_trader.common.factories import OrderFactory` | Order construction factory |
| `QuoteTick` | `from nautilus_trader.model.data import QuoteTick` | Top-of-book market event |
| `TradeTick` | `from nautilus_trader.model.data import TradeTick` | Executed trade tick event |
| `OrderBook` | `from nautilus_trader.model.data import OrderBook` | Level-2 depth snapshot |
| `OptionGreeks` | `from nautilus_trader.model.data import OptionGreeks` | Real-time option Greeks from venue |

---

## 2. Verified Strategy Callback Signatures

```python
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.data import QuoteTick, TradeTick, OrderBook, OptionGreeks
from nautilus_trader.model.events import (
    OrderSubmitted,
    OrderAccepted,
    OrderRejected,
    OrderCanceled,
    OrderCancelRejected,
    OrderExpired,
    OrderFilled,
    OrderUpdated,
    PositionOpened,
    PositionChanged,
    PositionClosed,
)

class ValidatedCallbackReference(Strategy):
    def on_start(self) -> None: ...
    def on_stop(self) -> None: ...
    def on_resume(self) -> None: ...
    def on_instrument(self, instrument: Instrument) -> None: ...
    
    # Market Data Feeds
    def on_quote_tick(self, tick: QuoteTick) -> None: ...
    def on_trade_tick(self, tick: TradeTick) -> None: ...
    def on_order_book(self, order_book: OrderBook) -> None: ...
    def on_option_greeks(self, option_greeks: OptionGreeks) -> None: ...
    
    # Order Lifecycle Events
    def on_order_submitted(self, event: OrderSubmitted) -> None: ...
    def on_order_accepted(self, event: OrderAccepted) -> None: ...
    def on_order_rejected(self, event: OrderRejected) -> None: ...
    def on_order_canceled(self, event: OrderCanceled) -> None: ...
    def on_order_cancel_rejected(self, event: OrderCancelRejected) -> None: ...
    def on_order_expired(self, event: OrderExpired) -> None: ...
    def on_order_filled(self, event: OrderFilled) -> None: ...
    def on_order_updated(self, event: OrderUpdated) -> None: ...
    
    # Position Lifecycle Events
    def on_position_opened(self, event: PositionOpened) -> None: ...
    def on_position_changed(self, event: PositionChanged) -> None: ...
    def on_position_closed(self, event: PositionClosed) -> None: ...
```

---

## 3. OrderFactory Method Signatures

Accessible via `self.order_factory` within any `Strategy` instance:

### 3.1. Limit Order
```python
order = self.order_factory.limit(
    instrument_id: InstrumentId,
    order_side: OrderSide,
    quantity: Quantity,
    price: Price,
    time_in_force: TimeInForce = TimeInForce.GTC,
    expire_time: datetime | None = None,
    post_only: bool = False,
    reduce_only: bool = False,
    quote_quantity: bool = False,
    display_qty: Quantity | None = None,
    emulation_trigger: TriggerType = TriggerType.NO_TRIGGER,
    trigger_instrument_id: InstrumentId | None = None,
    exec_algorithm_id: ExecAlgorithmId | None = None,
    exec_algorithm_params: dict | None = None,
    tags: list[str] | None = None,
    client_order_id: ClientOrderId | None = None,
)
```

### 3.2. Market Order
```python
order = self.order_factory.market(
    instrument_id: InstrumentId,
    order_side: OrderSide,
    quantity: Quantity,
    time_in_force: TimeInForce = TimeInForce.GTC,
    reduce_only: bool = False,
    quote_quantity: bool = False,
    exec_algorithm_id: ExecAlgorithmId | None = None,
    exec_algorithm_params: dict | None = None,
    tags: list[str] | None = None,
    client_order_id: ClientOrderId | None = None,
)
```

### 3.3. Stop Market Order
```python
order = self.order_factory.stop_market(
    instrument_id: InstrumentId,
    order_side: OrderSide,
    quantity: Quantity,
    trigger_price: Price,
    trigger_type: TriggerType = TriggerType.DEFAULT,
    time_in_force: TimeInForce = TimeInForce.GTC,
    expire_time: datetime | None = None,
    reduce_only: bool = False,
    quote_quantity: bool = False,
    client_order_id: ClientOrderId | None = None,
)
```

---

## 4. Complete Verified Testnet Bootstrapping Snippet

This complete, verified script demonstrates configuring NautilusTrader with Bybit Testnet for Options and Linear Perpetuals:

```python
from nautilus_trader.live.node import TradingNode
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.adapters.bybit.config import (
    BybitDataClientConfig,
    BybitExecClientConfig,
    BybitEnvironment,
    BybitProductType,
)
from nautilus_trader.adapters.bybit.factories import (
    BybitLiveDataClientFactory,
    BybitLiveExecClientFactory,
)

def create_bybit_testnet_node(api_key: str, api_secret: str) -> TradingNode:
    """Build and configure a live TradingNode for Bybit Testnet."""
    # 1. Bybit Data Client Configuration
    data_config = BybitDataClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        environment=BybitEnvironment.TESTNET,
        product_types=(
            BybitProductType.OPTION,
            BybitProductType.LINEAR,
        ),
        update_instruments_interval_mins=60,
        recv_window_ms=5000,
    )

    # 2. Bybit Execution Client Configuration
    exec_config = BybitExecClientConfig(
        api_key=api_key,
        api_secret=api_secret,
        environment=BybitEnvironment.TESTNET,
        product_types=(
            BybitProductType.OPTION,
            BybitProductType.LINEAR,
        ),
        use_ws_execution_fast=True,
        recv_window_ms=5000,
    )

    # 3. Node Configuration
    node_config = TradingNodeConfig(
        trader_id="BTC-STRADDLE-BOT-01",
        data_clients={"BYBIT": data_config},
        exec_clients={"BYBIT": exec_config},
        timeout_connection=15.0,
        timeout_reconciliation=10.0,
    )

    # 4. Initialize Node and Register Factories
    node = TradingNode(config=node_config)
    node.add_data_client_factory("BYBIT", BybitLiveDataClientFactory)
    node.add_exec_client_factory("BYBIT", BybitLiveExecClientFactory)

    # 5. Build Engines and Internal Topology
    node.build()
    return node
```
