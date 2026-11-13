"""
Unit tests for ATM Strike and Option Instrument Selector module.

Verifies:
1. Exact ATM strike selection: K* = S.
2. Equal-distance strike ties and deterministic tie-breaking (LOWER vs UPPER).
3. Expiration selection policies: NEAREST, TARGET_DTE, and EXPLICIT_DATE.
4. Handling and rejection of incomplete straddle pairs (missing Call or missing Put).
5. Filtering of expired contracts and minimum DTE thresholds.
6. Rejection of empty instrument chains.
7. Contract cross-validation (identical strike, identical expiry, Call/Put typing).
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
import pytest

from exchange.instrument_discovery import (
    DiscoveredOptionsChain,
    OptionContractDetails,
)
from strategy.instrument_selector import (
    ExpirySelectionPolicy,
    IncompletePairError,
    InstrumentSelector,
    MismatchedPairError,
    NoActiveInstrumentsError,
    NoValidExpiryError,
    SelectedStraddleLegs,
    TieBreakerPolicy,
)


def make_mock_option_details(
    symbol_str: str,
    option_type: str,
    strike_val: float,
    expiration_dt: datetime,
    is_active: bool = True,
    trading_status: str = "TRADING",
) -> OptionContractDetails:
    """Helper to construct verified OptionContractDetails for testing."""
    return OptionContractDetails(
        underlying="BTC",
        instrument_id=f"{symbol_str}.BYBIT",
        raw_symbol=symbol_str,
        option_type=option_type.upper(),
        strike_price=strike_val,
        expiration_utc=expiration_dt,
        contract_multiplier=1.0,
        settlement_currency="USDT",
        min_order_quantity=0.01,
        quantity_increment=0.01,
        price_precision=0,
        price_increment=5.0,
        trading_status=trading_status,
        is_active=is_active,
        nautilus_instrument=None,
    )


def build_mock_chain(
    pairs: list[tuple[datetime, float, bool, bool]],  # (expiry, strike, has_call, has_put)
    underlying: str = "BTC",
    as_of_utc: Optional[datetime] = None,
) -> DiscoveredOptionsChain:
    """Helper to build an indexed DiscoveredOptionsChain from a specification list."""
    now = as_of_utc or datetime.now(timezone.utc)
    calls: list[OptionContractDetails] = []
    puts: list[OptionContractDetails] = []
    contracts_by_id: dict[str, OptionContractDetails] = {}
    calls_map: dict[tuple[datetime, float], OptionContractDetails] = {}
    puts_map: dict[tuple[datetime, float], OptionContractDetails] = {}
    strikes_by_expiry: dict[datetime, list[float]] = {}

    for exp, strike, has_call, has_put in pairs:
        exp_str = exp.strftime("%d%b%y").upper()
        if exp not in strikes_by_expiry:
            strikes_by_expiry[exp] = []
        if strike not in strikes_by_expiry[exp]:
            strikes_by_expiry[exp].append(strike)

        if has_call:
            c = make_mock_option_details(f"BTC-{exp_str}-{int(strike)}-C-USDT", "CALL", strike, exp)
            calls.append(c)
            contracts_by_id[c.instrument_id] = c
            calls_map[(exp, strike)] = c

        if has_put:
            p = make_mock_option_details(f"BTC-{exp_str}-{int(strike)}-P-USDT", "PUT", strike, exp)
            puts.append(p)
            contracts_by_id[p.instrument_id] = p
            puts_map[(exp, strike)] = p

    for exp in strikes_by_expiry:
        strikes_by_expiry[exp].sort()

    expirations = sorted(strikes_by_expiry.keys())

    return DiscoveredOptionsChain(
        underlying=underlying,
        as_of_utc=now,
        total_discovered=len(calls) + len(puts),
        total_active=len(calls) + len(puts),
        calls=calls,
        puts=puts,
        expirations=expirations,
        strikes_by_expiry=strikes_by_expiry,
        contracts_by_id=contracts_by_id,
        _calls_map=calls_map,
        _puts_map=puts_map,
    )


def test_exact_atm_selection():
    """Verify that when spot price matches a strike exactly, that strike is chosen."""
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    exp = now + timedelta(days=7)

    strikes = [95000.0, 97500.0, 100000.0, 102500.0, 105000.0]
    pairs = [(exp, k, True, True) for k in strikes]
    chain = build_mock_chain(pairs, as_of_utc=now)

    selector = InstrumentSelector()
    selected = selector.select_straddle_pair(chain=chain, spot_price=100000.0, as_of_utc=now)

    assert selected.strike_price == 100000.0
    assert selected.moneyness_abs == 0.0
    assert selected.moneyness_pct == 0.0
    assert selected.call.option_type == "CALL"
    assert selected.put.option_type == "PUT"
    assert selected.call.strike_price == 100000.0
    assert selected.put.strike_price == 100000.0


def test_atm_between_two_strikes_tie_breaking():
    """Verify deterministic tie-breaking when spot price is equidistant between two strikes."""
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    exp = now + timedelta(days=7)

    # Spot is 100,000; available strikes are 97,500 and 102,500 (distance = 2,500 each)
    strikes = [97500.0, 102500.0]
    pairs = [(exp, k, True, True) for k in strikes]
    chain = build_mock_chain(pairs, as_of_utc=now)

    # Test LOWER policy (default)
    selector_lower = InstrumentSelector(tie_breaker=TieBreakerPolicy.LOWER)
    res_lower = selector_lower.select_straddle_pair(chain=chain, spot_price=100000.0, as_of_utc=now)
    assert res_lower.strike_price == 97500.0
    assert res_lower.moneyness_abs == 2500.0

    # Test UPPER policy
    selector_upper = InstrumentSelector(tie_breaker=TieBreakerPolicy.UPPER)
    res_upper = selector_upper.select_straddle_pair(chain=chain, spot_price=100000.0, as_of_utc=now)
    assert res_upper.strike_price == 102500.0
    assert res_upper.moneyness_abs == 2500.0


def test_multiple_expiries_selection_policies():
    """Verify selection behavior across multiple expiries using NEAREST and TARGET_DTE."""
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    exp1 = now + timedelta(days=1)
    exp2 = now + timedelta(days=7)
    exp3 = now + timedelta(days=30)

    pairs = [
        (exp1, 90000.0, True, True),
        (exp2, 90000.0, True, True),
        (exp3, 90000.0, True, True),
    ]
    chain = build_mock_chain(pairs, as_of_utc=now)

    # Policy 1: NEAREST -> chooses exp1 (1 day DTE)
    sel_nearest = InstrumentSelector(expiry_policy=ExpirySelectionPolicy.NEAREST)
    res_nearest = sel_nearest.select_straddle_pair(chain, spot_price=90000.0, as_of_utc=now)
    assert res_nearest.expiration_utc == exp1

    # Policy 2: TARGET_DTE (target=8 days) -> closest is exp2 (7 days DTE)
    sel_target = InstrumentSelector(
        expiry_policy=ExpirySelectionPolicy.TARGET_DTE,
        target_dte_days=8.0,
    )
    res_target = sel_target.select_straddle_pair(chain, spot_price=90000.0, as_of_utc=now)
    assert res_target.expiration_utc == exp2

    # Policy 3: EXPLICIT_DATE -> matches exp3
    sel_explicit = InstrumentSelector(
        expiry_policy=ExpirySelectionPolicy.EXPLICIT_DATE,
        explicit_expiry=exp3.strftime("%Y-%m-%d"),
    )
    res_explicit = sel_explicit.select_straddle_pair(chain, spot_price=90000.0, as_of_utc=now)
    assert res_explicit.expiration_utc == exp3


def test_missing_call_rejection():
    """Verify that a strike missing a Call is rejected when fallback is disabled."""
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    exp = now + timedelta(days=5)

    # 100,000 has Put only!
    pairs = [(exp, 100000.0, False, True)]
    chain = build_mock_chain(pairs, as_of_utc=now)

    selector = InstrumentSelector(allow_fallback_to_next_nearest=False)
    with pytest.raises(IncompletePairError, match="lacks a matching Call"):
        selector.select_straddle_pair(chain, spot_price=100000.0, as_of_utc=now)


def test_missing_put_rejection():
    """Verify that a strike missing a Put is rejected when fallback is disabled."""
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    exp = now + timedelta(days=5)

    # 100,000 has Call only!
    pairs = [(exp, 100000.0, True, False)]
    chain = build_mock_chain(pairs, as_of_utc=now)

    selector = InstrumentSelector(allow_fallback_to_next_nearest=False)
    with pytest.raises(IncompletePairError, match="lacks a matching Put"):
        selector.select_straddle_pair(chain, spot_price=100000.0, as_of_utc=now)


def test_fallback_to_next_nearest_complete_pair():
    """Verify that when absolute nearest strike is incomplete, selector safely advances to next nearest complete pair."""
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    exp = now + timedelta(days=5)

    # 100,000 is closest to spot (100,100) but lacks Put.
    # 98,000 has BOTH Call and Put!
    pairs = [
        (exp, 100000.0, True, False),  # Incomplete
        (exp, 98000.0, True, True),    # Complete
    ]
    chain = build_mock_chain(pairs, as_of_utc=now)

    selector = InstrumentSelector(allow_fallback_to_next_nearest=True)
    selected = selector.select_straddle_pair(chain, spot_price=100100.0, as_of_utc=now)

    assert selected.strike_price == 98000.0
    assert selected.call.strike_price == 98000.0
    assert selected.put.strike_price == 98000.0


def test_expired_instruments_filtered():
    """Verify that expired expiries (< now + min_dte_hours) are ignored."""
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    expired_exp = now - timedelta(hours=1)
    imminent_exp = now + timedelta(minutes=30)  # less than min_dte_hours (2.0)
    valid_exp = now + timedelta(days=3)

    pairs = [
        (expired_exp, 80000.0, True, True),
        (imminent_exp, 80000.0, True, True),
        (valid_exp, 80000.0, True, True),
    ]
    chain = build_mock_chain(pairs, as_of_utc=now)

    selector = InstrumentSelector(min_dte_hours=2.0)
    selected = selector.select_straddle_pair(chain, spot_price=80000.0, as_of_utc=now)

    assert selected.expiration_utc == valid_exp


def test_empty_instrument_chain_raises():
    """Verify that an empty options chain raises NoActiveInstrumentsError."""
    empty_chain = DiscoveredOptionsChain(
        underlying="BTC",
        as_of_utc=datetime.now(timezone.utc),
        total_discovered=0,
        total_active=0,
    )
    selector = InstrumentSelector()
    with pytest.raises(NoActiveInstrumentsError):
        selector.select_straddle_pair(empty_chain, spot_price=90000.0)
