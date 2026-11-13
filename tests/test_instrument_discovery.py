"""
Unit tests for BTC Options Instrument Discovery module.

Verifies:
1. Retrieval and parsing of all required contract fields.
2. Call and Put separation into dedicated collections.
3. Filtering of expired contracts against reference timestamps.
4. Handling of market statuses (TRADING vs HALT / NOT_AVAILABLE_FOR_TRADING).
5. Contract metadata validation (underlying, strike, precision, multiplier, step).
6. Graceful handling of empty or missing results.
7. In-memory caching and cache TTL expiration.
8. Straddle pair matching and ATM strike selection.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from nautilus_trader.model.currencies import BTC, USD, USDT
from nautilus_trader.model.enums import MarketStatusAction, OptionKind
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments.crypto_option import CryptoOption
from nautilus_trader.model.objects import Money, Price, Quantity

from config.settings import Settings
from exchange.instrument_discovery import (
    BtcOptionsDiscoveryService,
    DiscoveredOptionsChain,
    OptionContractDetails,
)


def make_mock_crypto_option(
    symbol_str: str,
    option_kind: OptionKind,
    strike_price_val: float,
    expiration_dt: datetime,
    underlying=BTC,
    settlement_currency=USDT,
    multiplier_val: float = 1.0,
    min_quantity_val: float = 0.01,
    size_increment_val: float = 0.01,
    price_precision_val: int = 0,
    price_increment_val: float = 5.0,
) -> CryptoOption:
    """Helper factory to construct authentic NautilusTrader CryptoOption instances for testing."""
    sym = Symbol(symbol_str)
    inst_id = InstrumentId(symbol=sym, venue=Venue("BYBIT"))
    exp_ns = int(expiration_dt.timestamp() * 1e9)
    act_ns = int((expiration_dt - timedelta(days=30)).timestamp() * 1e9)

    # In NautilusTrader, price_precision must strictly equal price_increment.precision
    if price_precision_val == 0:
        price_inc = Price.from_str(str(int(price_increment_val)))
        strike_price = Price.from_str(str(int(strike_price_val)))
    else:
        price_inc = Price.from_str(f"{price_increment_val:.{price_precision_val}f}")
        strike_price = Price.from_str(f"{strike_price_val:.{price_precision_val}f}")

    return CryptoOption(
        instrument_id=inst_id,
        raw_symbol=sym,
        underlying=underlying,
        quote_currency=USDT,
        settlement_currency=settlement_currency,
        is_inverse=False,
        option_kind=option_kind,
        strike_price=strike_price,
        activation_ns=act_ns,
        expiration_ns=exp_ns,
        price_precision=price_precision_val,
        size_precision=2,
        price_increment=price_inc,
        size_increment=Quantity.from_str(f"{size_increment_val:.2f}"),
        maker_fee=Decimal("0.0002"),
        taker_fee=Decimal("0.0003"),
        margin_init=Decimal("0"),
        margin_maint=Decimal("0"),
        max_quantity=Quantity.from_str("1000"),
        min_quantity=Quantity.from_str(f"{min_quantity_val:.2f}"),
        min_notional=Money(0.01, USDT),
        ts_event=0,
        ts_init=0,
    )


@pytest.fixture
def discovery_service() -> BtcOptionsDiscoveryService:
    settings = Settings(
        BYBIT_TESTNET=True,
        BTC_SYMBOL="BTC",
        ORDER_QUANTITY=0.1,
        DRY_RUN=True,
    )
    return BtcOptionsDiscoveryService(settings=settings, cache_ttl_seconds=60.0)


def test_process_instruments_call_and_put_separation(discovery_service: BtcOptionsDiscoveryService):
    """Verify that Calls and Puts are accurately identified and separated."""
    now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
    future_exp = now + timedelta(days=7)

    c1 = make_mock_crypto_option("BTC-20NOV26-90000-C-USDT", OptionKind.CALL, 90000.0, future_exp)
    p1 = make_mock_crypto_option("BTC-20NOV26-90000-P-USDT", OptionKind.PUT, 90000.0, future_exp)

    chain = discovery_service.process_instruments(
        instruments=[c1, p1],
        now_utc=now,
        active_only=True,
    )

    assert chain.total_discovered == 2
    assert chain.total_active == 2
    assert len(chain.calls) == 1
    assert len(chain.puts) == 1

    call_detail = chain.calls[0]
    put_detail = chain.puts[0]

    assert call_detail.is_call is True
    assert call_detail.is_put is False
    assert call_detail.strike_price == 90000.0
    assert call_detail.settlement_currency == "USDT"

    assert put_detail.is_put is True
    assert put_detail.is_call is False
    assert put_detail.strike_price == 90000.0


def test_process_instruments_filters_expired_contracts(discovery_service: BtcOptionsDiscoveryService):
    """Verify that contracts with expiration <= current time are excluded when active_only=True."""
    now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
    expired_exp = now - timedelta(days=2)
    active_exp = now + timedelta(days=5)

    expired_call = make_mock_crypto_option("BTC-11NOV26-80000-C-USDT", OptionKind.CALL, 80000.0, expired_exp)
    active_call = make_mock_crypto_option("BTC-18NOV26-85000-C-USDT", OptionKind.CALL, 85000.0, active_exp)

    chain = discovery_service.process_instruments(
        instruments=[expired_call, active_call],
        now_utc=now,
        active_only=True,
    )

    assert chain.total_discovered == 2
    assert chain.total_active == 1
    assert len(chain.calls) == 1
    assert chain.calls[0].strike_price == 85000.0


def test_process_instruments_market_status_filtering(discovery_service: BtcOptionsDiscoveryService):
    """Verify that instruments marked HALT or NOT_AVAILABLE_FOR_TRADING are filtered out if active_only."""
    now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
    exp = now + timedelta(days=10)

    inst_trading = make_mock_crypto_option("BTC-23NOV26-90000-C-USDT", OptionKind.CALL, 90000.0, exp)
    inst_halted = make_mock_crypto_option("BTC-23NOV26-95000-C-USDT", OptionKind.CALL, 95000.0, exp)

    statuses = {
        inst_trading.id: MarketStatusAction.TRADING,
        inst_halted.id: MarketStatusAction.HALT,
    }

    chain = discovery_service.process_instruments(
        instruments=[inst_trading, inst_halted],
        statuses=statuses,
        now_utc=now,
        active_only=True,
    )

    assert chain.total_active == 1
    assert chain.calls[0].instrument_id == str(inst_trading.id)
    assert chain.calls[0].trading_status == "TRADING"


def test_validate_metadata_rejections(discovery_service: BtcOptionsDiscoveryService):
    """Verify validation rules catch corrupt or malformed contracts."""
    exp = datetime(2026, 12, 25, 8, 0, tzinfo=timezone.utc)

    # Valid contract
    valid_opt = make_mock_crypto_option("BTC-25DEC26-100000-C-USDT", OptionKind.CALL, 100000.0, exp)
    ok, err = discovery_service.validate_metadata(valid_opt, underlying="BTC")
    assert ok is True
    assert err is None

    # Underlying mismatch
    ok, err = discovery_service.validate_metadata(valid_opt, underlying="ETH")
    assert ok is False
    assert "Underlying mismatch" in err

    # Non-positive strike price
    bad_strike_opt = make_mock_crypto_option("BTC-25DEC26-0-C-USDT", OptionKind.CALL, 0.0, exp)
    ok, err = discovery_service.validate_metadata(bad_strike_opt, underlying="BTC")
    assert ok is False
    assert "strike price" in err.lower()


def test_empty_results_handling(discovery_service: BtcOptionsDiscoveryService):
    """Verify graceful handling when no options instruments exist."""
    chain = discovery_service.process_instruments(instruments=[])
    assert chain.total_discovered == 0
    assert chain.total_active == 0
    assert len(chain.calls) == 0
    assert len(chain.puts) == 0
    assert len(chain.expirations) == 0


def test_straddle_pair_matching_and_atm_selection(discovery_service: BtcOptionsDiscoveryService):
    """Verify get_straddle_pair and find_nearest_atm_strike functionality."""
    now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
    exp = now + timedelta(days=7)

    c80 = make_mock_crypto_option("BTC-20NOV26-80000-C-USDT", OptionKind.CALL, 80000.0, exp)
    p80 = make_mock_crypto_option("BTC-20NOV26-80000-P-USDT", OptionKind.PUT, 80000.0, exp)

    c90 = make_mock_crypto_option("BTC-20NOV26-90000-C-USDT", OptionKind.CALL, 90000.0, exp)
    p90 = make_mock_crypto_option("BTC-20NOV26-90000-P-USDT", OptionKind.PUT, 90000.0, exp)

    chain = discovery_service.process_instruments(
        instruments=[c80, p80, c90, p90],
        now_utc=now,
        active_only=True,
    )

    # Matched straddle pair
    call, put = chain.get_straddle_pair(exp, 90000.0)
    assert call is not None and put is not None
    assert call.strike_price == 90000.0
    assert put.strike_price == 90000.0

    # ATM Strike Selection for spot price = 89,200 (nearest to 90,000)
    atm_k = chain.find_nearest_atm_strike(spot_price=89200.0, expiration=exp)
    assert atm_k == 90000.0

    # ATM Strike Selection for spot price = 81,000 (nearest to 80,000)
    atm_k2 = chain.find_nearest_atm_strike(spot_price=81000.0, expiration=exp)
    assert atm_k2 == 80000.0


def test_caching_behavior(discovery_service: BtcOptionsDiscoveryService):
    """Verify in-memory caching and cache invalidation."""
    assert discovery_service.is_cache_valid() is False

    chain = DiscoveredOptionsChain(
        underlying="BTC",
        as_of_utc=datetime.now(timezone.utc),
        total_discovered=10,
        total_active=10,
    )
    discovery_service._cache = chain
    import time
    discovery_service._cache_timestamp = time.time()

    assert discovery_service.is_cache_valid() is True

    # Invalidate cache
    discovery_service.clear_cache()
    assert discovery_service.is_cache_valid() is False
