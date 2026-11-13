"""
Unit tests for Straddle Premium & Risk Valuation Engine (Part 10).

Verifies:
1. Normal entry premium calculations (fixed Decimal precision).
2. Asymmetric leg quantities (different Call and Put executed quantities).
3. Contract multiplier conversions (standard 1.0 vs custom scaling).
4. Stop-Loss threshold calculation (1.01 * ENTRY_PREMIUM).
5. Take-Profit threshold calculation (0.98 * ENTRY_PREMIUM).
6. Live buyback valuation using strict Ask quotes.
7. Missing quotes handling (None, zero, negative Ask prices).
8. PnL and trigger conditions (SL breach, TP reach).
9. Invalid data handling (negative prices, negative quantities, NaN/Inf).
10. StraddleCalculator and StraddlePremiumSnapshot integration.
"""

from decimal import Decimal
import math
import pytest

from nautilus_trader.model.objects import Price, Quantity

from utils.calculations import (
    CalculationError,
    DEFAULT_CONTRACT_MULTIPLIER,
    DEFAULT_SL_FACTOR,
    DEFAULT_TP_FACTOR,
    InvalidCalculationInputError,
    MissingQuoteError,
    StraddleCalculator,
    StraddlePremiumSnapshot,
    calculate_entry_premium,
    calculate_leg_premium,
    calculate_live_buyback_value,
    calculate_pnl_percent,
    calculate_stop_loss_threshold,
    calculate_take_profit_threshold,
    calculate_unrealized_pnl,
    is_stop_loss_triggered,
    is_take_profit_triggered,
    to_decimal,
)


# -----------------------------------------------------------------------------
# 1. Type Coercion & Input Normalization Tests
# -----------------------------------------------------------------------------


def test_to_decimal_various_types():
    """Verify to_decimal converts floats, ints, strings, Decimals, and Nautilus types."""
    assert to_decimal(100) == Decimal("100")
    assert to_decimal(1250.5) == Decimal("1250.5")
    assert to_decimal("240.25") == Decimal("240.25")
    assert to_decimal(Decimal("350.00")) == Decimal("350.00")

    # NautilusTrader Price and Quantity
    nautilus_px = Price.from_str("68500.50")
    nautilus_qty = Quantity.from_str("0.15")
    assert to_decimal(nautilus_px) == Decimal("68500.5")
    assert to_decimal(nautilus_qty) == Decimal("0.15")


def test_to_decimal_invalid_inputs():
    """Verify to_decimal rejects None (unless allowed), NaN, Inf, and bad strings."""
    with pytest.raises(InvalidCalculationInputError, match="cannot be None"):
        to_decimal(None, "test_field")

    assert to_decimal(None, "test_field", allow_none=True) is None

    with pytest.raises(InvalidCalculationInputError, match="non-finite"):
        to_decimal(float("nan"), "nan_field")

    with pytest.raises(InvalidCalculationInputError, match="non-finite"):
        to_decimal(float("inf"), "inf_field")

    with pytest.raises(InvalidCalculationInputError, match="cannot be parsed"):
        to_decimal("invalid-number", "bad_str")


# -----------------------------------------------------------------------------
# 2. Leg Premium & Entry Premium Calculations (Normal)
# -----------------------------------------------------------------------------


def test_leg_premium_calculation_normal():
    """
    Verify calculate_leg_premium:
    Formula: PRICE * QUANTITY * MULTIPLIER
    Example: 1250.00 USDT/BTC * 0.10 BTC * 1.0 = 125.0000 USDT
    """
    premium = calculate_leg_premium(
        price=1250.00,
        quantity=0.10,
        multiplier=1.0,
    )
    assert premium == Decimal("125.0000")
    assert isinstance(premium, Decimal)


def test_entry_premium_equal_quantities():
    """
    Verify calculate_entry_premium for balanced short straddle:
    Call: 0.10 BTC @ 1250.00 USDT -> 125.00 USDT
    Put:  0.10 BTC @ 1150.00 USDT -> 115.00 USDT
    Total Entry Premium: 240.00 USDT
    """
    entry_prem = calculate_entry_premium(
        call_entry_price=1250.00,
        call_quantity=0.10,
        put_entry_price=1150.00,
        put_quantity=0.10,
        call_multiplier=1.0,
        put_multiplier=1.0,
    )
    assert entry_prem == Decimal("240.0000")


# -----------------------------------------------------------------------------
# 3. Asymmetric Leg Quantities (Partial Fills & Imbalances)
# -----------------------------------------------------------------------------


def test_entry_premium_different_leg_quantities():
    """
    CRITICAL REQUIREMENT:
    Verify entry premium calculation uses actual executed quantities,
    not requested order quantities.
    Scenario: Call filled 0.10 BTC, Put filled only 0.05 BTC (partial fill).
    Call: 0.10 * 1250 = 125.00
    Put:  0.05 * 1150 = 57.50
    Total Entry Premium: 182.50 USDT
    """
    entry_prem = calculate_entry_premium(
        call_entry_price=1250.00,
        call_quantity=0.10,
        put_entry_price=1150.00,
        put_quantity=0.05,
    )
    assert entry_prem == Decimal("182.5000")


def test_entry_premium_zero_quantity_single_leg():
    """Scenario: Only Call filled (0.10 BTC), Put filled 0.00 BTC."""
    entry_prem = calculate_entry_premium(
        call_entry_price=1250.00,
        call_quantity=0.10,
        put_entry_price=1150.00,
        put_quantity=0.00,
    )
    assert entry_prem == Decimal("125.0000")


# -----------------------------------------------------------------------------
# 4. Contract Multiplier Conversion Tests
# -----------------------------------------------------------------------------


def test_contract_multiplier_conversion():
    """
    Verify contract multiplier scaling:
    Bybit BTC options default to 1.0 (1 contract = 1 BTC).
    Custom multipliers (e.g. 10.0, 0.5, 100.0) must scale cash value accordingly.
    """
    # Multiplier = 10.0 (e.g. mini index)
    # Price = 50.0, Qty = 2 contracts -> 50 * 2 * 10 = 1000.0 USDT
    call_prem = calculate_leg_premium(price=50.0, quantity=2, multiplier=10.0)
    assert call_prem == Decimal("1000.0000")

    # Entry premium with multiplier = 0.5
    # Call: 1000 * 1 * 0.5 = 500
    # Put:  800 * 1 * 0.5 = 400
    # Total = 900
    entry_prem = calculate_entry_premium(
        call_entry_price=1000.0,
        call_quantity=1.0,
        put_entry_price=800.0,
        put_quantity=1.0,
        call_multiplier=0.5,
        put_multiplier=0.5,
    )
    assert entry_prem == Decimal("900.0000")


# -----------------------------------------------------------------------------
# 5. Stop-Loss & Take-Profit Threshold Calculations
# -----------------------------------------------------------------------------


def test_stop_loss_threshold_calculation():
    """
    Verify SL_THRESHOLD = ENTRY_PREMIUM * 1.01
    Entry Premium = 240.00 USDT
    SL Threshold = 240.00 * 1.01 = 242.4000 USDT
    """
    entry_prem = Decimal("240.0000")
    sl_threshold = calculate_stop_loss_threshold(entry_prem)
    assert sl_threshold == Decimal("242.4000")

    # Custom SL multiplier (e.g. 1.05 = +5% loss limit)
    custom_sl = calculate_stop_loss_threshold(entry_prem, sl_multiplier=1.05)
    assert custom_sl == Decimal("252.0000")


def test_take_profit_threshold_calculation():
    """
    Verify TP_THRESHOLD = ENTRY_PREMIUM * 0.98
    Entry Premium = 240.00 USDT
    TP Threshold = 240.00 * 0.98 = 235.2000 USDT
    """
    entry_prem = Decimal("240.0000")
    tp_threshold = calculate_take_profit_threshold(entry_prem)
    assert tp_threshold == Decimal("235.2000")

    # Custom TP multiplier (e.g. 0.90 = -10% buyback cost / 10% profit)
    custom_tp = calculate_take_profit_threshold(entry_prem, tp_multiplier=0.90)
    assert custom_tp == Decimal("216.0000")


# -----------------------------------------------------------------------------
# 6. Live Buyback Valuation Using Strict Ask Quotes
# -----------------------------------------------------------------------------


def test_live_buyback_value_normal():
    """
    Verify CURRENT_BUYBACK_VALUE = (CALL_ASK * CALL_QTY) + (PUT_ASK * PUT_QTY)
    Call Ask = 1240.00, Call Qty = 0.10 -> 124.00
    Put Ask  = 1120.00, Put Qty  = 0.10 -> 112.00
    Buyback Value = 236.0000 USDT
    """
    buyback = calculate_live_buyback_value(
        call_ask=1240.00,
        call_quantity=0.10,
        put_ask=1120.00,
        put_quantity=0.10,
        call_multiplier=1.0,
        put_multiplier=1.0,
    )
    assert buyback == Decimal("236.0000")


def test_live_buyback_value_asymmetric_quantities():
    """
    Verify live buyback with different leg quantities:
    Call: 0.10 BTC @ 1240.00 Ask -> 124.00
    Put:  0.05 BTC @ 1120.00 Ask -> 56.00
    Total Buyback = 180.0000 USDT
    """
    buyback = calculate_live_buyback_value(
        call_ask=1240.00,
        call_quantity=0.10,
        put_ask=1120.00,
        put_quantity=0.05,
    )
    assert buyback == Decimal("180.0000")


# -----------------------------------------------------------------------------
# 7. Missing & Invalid Quotes Handling
# -----------------------------------------------------------------------------


def test_live_buyback_missing_quotes():
    """
    CRITICAL REQUIREMENT:
    Verify MissingQuoteError is raised when an open leg has a missing Ask quote.
    """
    # Missing Call Ask (None)
    with pytest.raises(MissingQuoteError, match="Call Ask quote is missing"):
        calculate_live_buyback_value(
            call_ask=None,
            call_quantity=0.10,
            put_ask=1120.00,
            put_quantity=0.10,
        )

    # Missing Put Ask (None)
    with pytest.raises(MissingQuoteError, match="Put Ask quote is missing"):
        calculate_live_buyback_value(
            call_ask=1240.00,
            call_quantity=0.10,
            put_ask=None,
            put_quantity=0.10,
        )

    # Non-positive Call Ask (<= 0)
    with pytest.raises(MissingQuoteError, match="must be strictly positive"):
        calculate_live_buyback_value(
            call_ask=0.0,
            call_quantity=0.10,
            put_ask=1120.00,
            put_quantity=0.10,
        )


def test_live_buyback_zero_quantity_leg_skips_quote_requirement():
    """If a leg has 0 open quantity (e.g. Put not open), its ask quote is not required."""
    buyback = calculate_live_buyback_value(
        call_ask=1240.00,
        call_quantity=0.10,
        put_ask=None,  # No Put quote
        put_quantity=0.00,  # 0 Put quantity open
    )
    assert buyback == Decimal("124.0000")


# -----------------------------------------------------------------------------
# 8. PnL & Trigger Evaluation
# -----------------------------------------------------------------------------


def test_unrealized_pnl_and_percentage():
    """
    Verify Unrealized PnL:
    Entry = 240.00 USDT
    Scenario 1: Buyback drops to 235.20 (TP level)
    PnL = 240.00 - 235.20 = +4.8000 USDT
    PnL % = (+4.80 / 240.00) * 100 = +2.00%
    """
    entry = Decimal("240.0000")
    buyback_tp = Decimal("235.2000")

    pnl_profit = calculate_unrealized_pnl(entry, buyback_tp)
    assert pnl_profit == Decimal("4.8000")

    pct_profit = calculate_pnl_percent(entry, buyback_tp)
    assert pct_profit == Decimal("2.00")

    # Scenario 2: Buyback rises to 242.40 (SL level)
    buyback_sl = Decimal("242.4000")
    pnl_loss = calculate_unrealized_pnl(entry, buyback_sl)
    assert pnl_loss == Decimal("-2.4000")

    pct_loss = calculate_pnl_percent(entry, buyback_sl)
    assert pct_loss == Decimal("-1.00")


def test_trigger_evaluations():
    """
    Verify is_stop_loss_triggered and is_take_profit_triggered:
    Entry = 240.00
    SL Threshold = 242.40
    TP Threshold = 235.20
    """
    sl_thresh = Decimal("242.4000")
    tp_thresh = Decimal("235.2000")

    # Normal range (Buyback = 238.00)
    assert not is_stop_loss_triggered(238.00, sl_thresh)
    assert not is_take_profit_triggered(238.00, tp_thresh)

    # Exact Take-Profit hit (235.20)
    assert is_take_profit_triggered(235.20, tp_thresh)
    assert not is_stop_loss_triggered(235.20, sl_thresh)

    # Beyond Take-Profit (230.00)
    assert is_take_profit_triggered(230.00, tp_thresh)

    # Exact Stop-Loss hit (242.40)
    assert is_stop_loss_triggered(242.40, sl_thresh)
    assert not is_take_profit_triggered(242.40, tp_thresh)

    # Beyond Stop-Loss (250.00)
    assert is_stop_loss_triggered(250.00, sl_thresh)


# -----------------------------------------------------------------------------
# 9. Invalid Inputs Rejection
# -----------------------------------------------------------------------------


def test_negative_inputs_rejection():
    """Verify negative prices, quantities, and multipliers are rejected."""
    with pytest.raises(InvalidCalculationInputError, match="cannot be negative"):
        calculate_leg_premium(price=-100.0, quantity=0.1)

    with pytest.raises(InvalidCalculationInputError, match="cannot be negative"):
        calculate_leg_premium(price=100.0, quantity=-0.1)

    with pytest.raises(InvalidCalculationInputError, match="strictly positive"):
        calculate_leg_premium(price=100.0, quantity=0.1, multiplier=0.0)

    with pytest.raises(InvalidCalculationInputError, match="strictly positive"):
        calculate_leg_premium(price=100.0, quantity=0.1, multiplier=-1.0)

    with pytest.raises(InvalidCalculationInputError, match="zero or negative entry premium"):
        calculate_pnl_percent(entry_premium=0.0, current_buyback_value=100.0)


# -----------------------------------------------------------------------------
# 10. StraddleCalculator & Snapshot Integration
# -----------------------------------------------------------------------------


def test_straddle_calculator_full_evaluation():
    """
    Verify StraddleCalculator end-to-end evaluation:
    - Entry premium calculation
    - Threshold generation
    - Buyback cost
    - PnL and breach status
    """
    calc = StraddleCalculator(
        contract_multiplier=Decimal("1.0"),
        sl_factor=Decimal("1.01"),
        tp_factor=Decimal("0.98"),
    )

    # 1. Normal active market (no breach)
    snapshot = calc.evaluate(
        call_entry_price=1250.00,
        call_executed_qty=0.10,
        put_entry_price=1150.00,
        put_executed_qty=0.10,
        call_ask=1240.00,
        put_ask=1130.00,
    )
    assert snapshot.total_entry_premium == Decimal("240.0000")
    assert snapshot.sl_threshold == Decimal("242.4000")
    assert snapshot.tp_threshold == Decimal("235.2000")
    assert snapshot.current_buyback_value == Decimal("237.0000")
    assert snapshot.unrealized_pnl == Decimal("3.0000")
    assert snapshot.pnl_percent == Decimal("1.25")
    assert snapshot.is_sl_triggered is False
    assert snapshot.is_tp_triggered is False
    assert snapshot.is_balanced is True

    # 2. Stop-Loss triggered evaluation
    snapshot_sl = calc.evaluate(
        call_entry_price=1250.00,
        call_executed_qty=0.10,
        put_entry_price=1150.00,
        put_executed_qty=0.10,
        call_ask=1300.00,  # Surged
        put_ask=1130.00,
    )
    assert snapshot_sl.current_buyback_value == Decimal("243.0000")
    assert snapshot_sl.is_sl_triggered is True
    assert snapshot_sl.is_tp_triggered is False

    # 3. Take-Profit triggered evaluation
    snapshot_tp = calc.evaluate(
        call_entry_price=1250.00,
        call_executed_qty=0.10,
        put_entry_price=1150.00,
        put_executed_qty=0.10,
        call_ask=1220.00,  # Decayed
        put_ask=1130.00,  # 122 + 113 = 235.00 <= 235.20
    )
    assert snapshot_tp.current_buyback_value == Decimal("235.0000")
    assert snapshot_tp.is_tp_triggered is True
    assert snapshot_tp.is_sl_triggered is False
