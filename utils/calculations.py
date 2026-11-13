"""
Straddle Premium & Risk Valuation Engine (Part 10).

Assignment: Mernplus Technologies BTC Options Straddle
Stage: Part 10 - Straddle Premium Calculations

This module implements institutional-grade, fixed-precision financial calculations
for options straddles on Bybit and NautilusTrader.

Core Formulas Implemented:
1. Entry Premium:
   ENTRY_PREMIUM = CALL_ENTRY_PREMIUM + PUT_ENTRY_PREMIUM
   Where:
     CALL_ENTRY_PREMIUM = CALL_ENTRY_VWAP * CALL_EXECUTED_QUANTITY * CALL_MULTIPLIER
     PUT_ENTRY_PREMIUM  = PUT_ENTRY_VWAP  * PUT_EXECUTED_QUANTITY  * PUT_MULTIPLIER

2. Stop-Loss Threshold:
   SL_THRESHOLD = ENTRY_PREMIUM * 1.01
   (Represents 1.0% adverse rise in buyback liability for short straddles)

3. Take-Profit Threshold:
   TP_THRESHOLD = ENTRY_PREMIUM * 0.98
   (Represents 2.0% favorable decline in buyback cost via theta decay)

4. Live Buyback Valuation:
   CURRENT_BUYBACK_VALUE = (CALL_ASK * CALL_EXECUTED_QUANTITY * CALL_MULTIPLIER) +
                           (PUT_ASK  * PUT_EXECUTED_QUANTITY  * PUT_MULTIPLIER)

Financial Invariants Enforced:
- Fixed-Precision Arithmetic: All core calculations use Python Decimal to eliminate
  IEEE 754 binary floating-point roundoff errors.
- Real Executed Quantities: Entry and exit valuations strictly utilize actual filled
  quantities, never unexecuted requested quantities.
- Asymmetric Leg Protection: Accurately computes valuations for imbalanced or partial fills.
- Strict Ask-Side Buyback Rule: Buyback liability strictly requires positive, non-zero Ask quotes.
  LTP (Last Traded Price), Bid, or Mid prices are strictly rejected.
- Contract Multiplier Fidelity: Supports arbitrary contract multipliers (defaults to 1.0 for
  Bybit BTC options, where 1 contract = 1.0 BTC).
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from typing import Any, Optional, Tuple, Union


# -----------------------------------------------------------------------------
# Precision & Constant Definitions
# -----------------------------------------------------------------------------

# Currency decimal precision (e.g. USDT settlement is typically 4-8 decimals, rounded to cents)
MONEY_DECIMALS = Decimal("0.0001")
PERCENT_DECIMALS = Decimal("0.01")

DEFAULT_SL_FACTOR = Decimal("1.01")  # 101% of entry premium (+1% loss threshold)
DEFAULT_TP_FACTOR = Decimal("0.98")  # 98% of entry premium (-2% profit target)
DEFAULT_CONTRACT_MULTIPLIER = Decimal("1.0")  # Bybit BTC option: 1 contract = 1 BTC


# -----------------------------------------------------------------------------
# Custom Exceptions
# -----------------------------------------------------------------------------


class CalculationError(Exception):
    """Base exception for straddle premium and risk calculations."""


class InvalidCalculationInputError(CalculationError):
    """Raised when calculation inputs are negative, non-finite, or mathematically invalid."""


class MissingQuoteError(CalculationError):
    """Raised when a required Ask quote is missing, None, or non-positive (<= 0)."""


# -----------------------------------------------------------------------------
# Type Coercion Helpers
# -----------------------------------------------------------------------------


def to_decimal(
    val: Union[Decimal, float, int, str, Any],
    field_name: str = "value",
    allow_none: bool = False,
) -> Optional[Decimal]:
    """
    Safely convert an input number to Python Decimal with rigorous sanity checks.

    Supports:
    - Decimal instances
    - Standard floats and integers
    - Numeric strings (e.g. "1250.50")
    - NautilusTrader Price and Quantity objects (via str conversion)

    Raises:
        InvalidCalculationInputError: If value is NaN, infinite, or unparseable.
    """
    if val is None:
        if allow_none:
            return None
        raise InvalidCalculationInputError(f"Field '{field_name}' cannot be None.")

    # NautilusTrader Price or Quantity object
    if hasattr(val, "as_double"):
        try:
            return Decimal(str(val.as_double()))
        except Exception:
            return Decimal(str(val))

    if isinstance(val, Decimal):
        if val.is_nan() or val.is_infinite():
            raise InvalidCalculationInputError(
                f"Field '{field_name}' has non-finite Decimal value: {val}."
            )
        return val

    if isinstance(val, (int,)):
        return Decimal(val)

    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise InvalidCalculationInputError(
                f"Field '{field_name}' has non-finite float value: {val}."
            )
        # Convert via string representation to avoid float representation artifacts
        return Decimal(str(val))

    if isinstance(val, str):
        clean_str = val.strip()
        if not clean_str:
            raise InvalidCalculationInputError(f"Field '{field_name}' string is empty.")
        try:
            d = Decimal(clean_str)
            if d.is_nan() or d.is_infinite():
                raise InvalidCalculationInputError(
                    f"Field '{field_name}' parsed non-finite value: {clean_str}."
                )
            return d
        except InvalidOperation as e:
            raise InvalidCalculationInputError(
                f"Field '{field_name}' cannot be parsed as Decimal: '{clean_str}' ({e})."
            )

    try:
        return Decimal(str(val))
    except Exception as e:
        raise InvalidCalculationInputError(
            f"Unsupported type for '{field_name}': {type(val)} ({e})."
        )


# -----------------------------------------------------------------------------
# Core Calculation Functions
# -----------------------------------------------------------------------------


def calculate_leg_premium(
    price: Union[Decimal, float, int, str],
    quantity: Union[Decimal, float, int, str],
    multiplier: Union[Decimal, float, int, str] = DEFAULT_CONTRACT_MULTIPLIER,
    field_prefix: str = "leg",
) -> Decimal:
    """
    Calculate the total cash premium value for a single option leg.

    Formula:
        PREMIUM = PRICE * QUANTITY * MULTIPLIER

    Units:
        Price:      Settlement Currency / Underlying (e.g. USDT per BTC)
        Quantity:   Contracts or Underlying units (e.g. BTC)
        Multiplier: Contract scaling factor (e.g. 1.0 for 1 BTC/contract)
        Result:     Total Cash Value in Settlement Currency (USDT)

    Invariants:
        price >= 0
        quantity >= 0
        multiplier > 0
    """
    p = to_decimal(price, f"{field_prefix}_price")
    q = to_decimal(quantity, f"{field_prefix}_quantity")
    m = to_decimal(multiplier, f"{field_prefix}_multiplier")

    if p < Decimal("0"):
        raise InvalidCalculationInputError(
            f"{field_prefix.capitalize()} price cannot be negative: {p}"
        )
    if q < Decimal("0"):
        raise InvalidCalculationInputError(
            f"{field_prefix.capitalize()} quantity cannot be negative: {q}"
        )
    if m <= Decimal("0"):
        raise InvalidCalculationInputError(
            f"{field_prefix.capitalize()} contract multiplier must be strictly positive: {m}"
        )

    return (p * q * m).quantize(MONEY_DECIMALS, rounding=ROUND_HALF_UP)


def calculate_entry_premium(
    call_entry_price: Union[Decimal, float, int, str],
    call_quantity: Union[Decimal, float, int, str],
    put_entry_price: Union[Decimal, float, int, str],
    put_quantity: Union[Decimal, float, int, str],
    call_multiplier: Union[Decimal, float, int, str] = DEFAULT_CONTRACT_MULTIPLIER,
    put_multiplier: Union[Decimal, float, int, str] = DEFAULT_CONTRACT_MULTIPLIER,
) -> Decimal:
    """
    Calculate the total gross entry premium collected upon selling the short straddle pair.

    Formula:
        ENTRY_PREMIUM = CALL_ENTRY_PREMIUM + PUT_ENTRY_PREMIUM
        Where:
            CALL_ENTRY_PREMIUM = CALL_PRICE * CALL_QUANTITY * CALL_MULTIPLIER
            PUT_ENTRY_PREMIUM  = PUT_PRICE  * PUT_QUANTITY  * PUT_MULTIPLIER

    IMPORTANT:
        - Must use actual executed fill quantities, not requested order quantities.
        - Handles imbalanced leg quantities (e.g. Call=0.10 BTC, Put=0.05 BTC) with precision.

    Returns:
        Total entry premium cash value in settlement currency (Decimal, quantized to 4 places).
    """
    call_prem = calculate_leg_premium(
        price=call_entry_price,
        quantity=call_quantity,
        multiplier=call_multiplier,
        field_prefix="call_entry",
    )
    put_prem = calculate_leg_premium(
        price=put_entry_price,
        quantity=put_quantity,
        multiplier=put_multiplier,
        field_prefix="put_entry",
    )

    return (call_prem + put_prem).quantize(MONEY_DECIMALS, rounding=ROUND_HALF_UP)


def calculate_stop_loss_threshold(
    entry_premium: Union[Decimal, float, int, str],
    sl_multiplier: Union[Decimal, float, int, str] = DEFAULT_SL_FACTOR,
) -> Decimal:
    """
    Calculate the Stop-Loss buyback cost threshold for a short straddle position.

    Formula:
        SL_THRESHOLD = ENTRY_PREMIUM * 1.01

    Economic Rationale:
        A short straddle collects cash credit upfront.
        The position loses money when the cost to buy back the legs increases.
        If current buyback cost rises above 101% of entry premium collected,
        the net loss exceeds 1.0%, triggering risk liquidation.

    Invariants:
        entry_premium >= 0
        sl_multiplier >= 1.0 (for stop-loss on short positions)
    """
    ep = to_decimal(entry_premium, "entry_premium")
    factor = to_decimal(sl_multiplier, "sl_multiplier")

    if ep < Decimal("0"):
        raise InvalidCalculationInputError(f"Entry premium cannot be negative: {ep}")
    if factor <= Decimal("0"):
        raise InvalidCalculationInputError(
            f"Stop-loss factor must be strictly positive: {factor}"
        )

    return (ep * factor).quantize(MONEY_DECIMALS, rounding=ROUND_HALF_UP)


def calculate_take_profit_threshold(
    entry_premium: Union[Decimal, float, int, str],
    tp_multiplier: Union[Decimal, float, int, str] = DEFAULT_TP_FACTOR,
) -> Decimal:
    """
    Calculate the Take-Profit buyback cost threshold for a short straddle position.

    Formula:
        TP_THRESHOLD = ENTRY_PREMIUM * 0.98

    Economic Rationale:
        As time elapses and theta decays, the options lose extrinsic value.
        When the aggregate cost to buy back both open legs drops to 98% of
        the initial entry premium collected, the trader captures a 2.0% profit,
        triggering target realization.

    Invariants:
        entry_premium >= 0
        0 < tp_multiplier <= 1.0 (for take-profit on short positions)
    """
    ep = to_decimal(entry_premium, "entry_premium")
    factor = to_decimal(tp_multiplier, "tp_multiplier")

    if ep < Decimal("0"):
        raise InvalidCalculationInputError(f"Entry premium cannot be negative: {ep}")
    if factor <= Decimal("0"):
        raise InvalidCalculationInputError(
            f"Take-profit factor must be strictly positive: {factor}"
        )

    return (ep * factor).quantize(MONEY_DECIMALS, rounding=ROUND_HALF_UP)


def calculate_live_buyback_value(
    call_ask: Optional[Union[Decimal, float, int, str]],
    call_quantity: Union[Decimal, float, int, str],
    put_ask: Optional[Union[Decimal, float, int, str]],
    put_quantity: Union[Decimal, float, int, str],
    call_multiplier: Union[Decimal, float, int, str] = DEFAULT_CONTRACT_MULTIPLIER,
    put_multiplier: Union[Decimal, float, int, str] = DEFAULT_CONTRACT_MULTIPLIER,
) -> Decimal:
    """
    Calculate the current live buyback liability required to liquidate open short option legs.

    Formula:
        CURRENT_BUYBACK_VALUE = (CALL_ASK * CALL_QUANTITY * CALL_MULTIPLIER) +
                                (PUT_ASK  * PUT_QUANTITY  * PUT_MULTIPLIER)

    MANDATORY FINANCIAL VALUATION RULE:
        - Short positions are liabilities that must be repurchased at the venue ASK price.
        - If an open leg (quantity > 0) has a missing quote (None) or non-positive Ask (<= 0),
          a MissingQuoteError is raised. We NEVER substitute LTP (Last Traded Price) or Bid.
        - If an open leg has quantity == 0 (e.g. Leg closed or never filled), its ask quote
          is not strictly required (buyback cost for 0 contracts is 0.00).

    Returns:
        Current buyback cost in settlement currency (Decimal, quantized to 4 places).
    """
    c_qty = to_decimal(call_quantity, "call_quantity")
    p_qty = to_decimal(put_quantity, "put_quantity")
    c_mult = to_decimal(call_multiplier, "call_multiplier")
    p_mult = to_decimal(put_multiplier, "put_multiplier")

    if c_qty < Decimal("0"):
        raise InvalidCalculationInputError(f"Call quantity cannot be negative: {c_qty}")
    if p_qty < Decimal("0"):
        raise InvalidCalculationInputError(f"Put quantity cannot be negative: {p_qty}")
    if c_mult <= Decimal("0") or p_mult <= Decimal("0"):
        raise InvalidCalculationInputError("Contract multipliers must be strictly positive.")

    # Call leg buyback cost
    call_buyback = Decimal("0.0")
    if c_qty > Decimal("0"):
        if call_ask is None:
            raise MissingQuoteError(
                "Call Ask quote is missing (None) while Call quantity > 0."
            )
        c_ask = to_decimal(call_ask, "call_ask")
        if c_ask <= Decimal("0"):
            raise MissingQuoteError(
                f"Call Ask price must be strictly positive for buyback valuation: {c_ask}"
            )
        call_buyback = c_ask * c_qty * c_mult

    # Put leg buyback cost
    put_buyback = Decimal("0.0")
    if p_qty > Decimal("0"):
        if put_ask is None:
            raise MissingQuoteError(
                "Put Ask quote is missing (None) while Put quantity > 0."
            )
        p_ask = to_decimal(put_ask, "put_ask")
        if p_ask <= Decimal("0"):
            raise MissingQuoteError(
                f"Put Ask price must be strictly positive for buyback valuation: {p_ask}"
            )
        put_buyback = p_ask * p_qty * p_mult

    return (call_buyback + put_buyback).quantize(MONEY_DECIMALS, rounding=ROUND_HALF_UP)


def calculate_unrealized_pnl(
    entry_premium: Union[Decimal, float, int, str],
    current_buyback_value: Union[Decimal, float, int, str],
) -> Decimal:
    """
    Calculate the current unrealized profit or loss (in settlement currency).

    Formula:
        UNREALIZED_PNL = ENTRY_PREMIUM - CURRENT_BUYBACK_VALUE

    Positive value indicates profit (buyback cost < entry premium received).
    Negative value indicates loss (buyback cost > entry premium received).
    """
    ep = to_decimal(entry_premium, "entry_premium")
    bv = to_decimal(current_buyback_value, "current_buyback_value")

    if ep < Decimal("0"):
        raise InvalidCalculationInputError(f"Entry premium cannot be negative: {ep}")
    if bv < Decimal("0"):
        raise InvalidCalculationInputError(f"Current buyback value cannot be negative: {bv}")

    return (ep - bv).quantize(MONEY_DECIMALS, rounding=ROUND_HALF_UP)


def calculate_pnl_percent(
    entry_premium: Union[Decimal, float, int, str],
    current_buyback_value: Union[Decimal, float, int, str],
) -> Decimal:
    """
    Calculate the percentage return on the initial entry premium collected.

    Formula:
        PNL_PERCENT = ((ENTRY_PREMIUM - CURRENT_BUYBACK_VALUE) / ENTRY_PREMIUM) * 100

    Raises:
        InvalidCalculationInputError: If entry_premium is zero or negative.
    """
    ep = to_decimal(entry_premium, "entry_premium")
    bv = to_decimal(current_buyback_value, "current_buyback_value")

    if ep <= Decimal("0"):
        raise InvalidCalculationInputError(
            f"Cannot calculate PnL percentage with zero or negative entry premium: {ep}"
        )
    if bv < Decimal("0"):
        raise InvalidCalculationInputError(f"Current buyback value cannot be negative: {bv}")

    pnl = ep - bv
    return ((pnl / ep) * Decimal("100")).quantize(PERCENT_DECIMALS, rounding=ROUND_HALF_UP)


def is_stop_loss_triggered(
    current_buyback_value: Union[Decimal, float, int, str],
    sl_threshold: Union[Decimal, float, int, str],
) -> bool:
    """
    Check if the Stop-Loss barrier is breached.

    Condition:
        CURRENT_BUYBACK_VALUE >= SL_THRESHOLD

    For short straddles, buyback cost rising to or above SL_THRESHOLD
    indicates an adverse move exceeding the allowable tolerance.
    """
    bv = to_decimal(current_buyback_value, "current_buyback_value")
    slt = to_decimal(sl_threshold, "sl_threshold")

    if bv < Decimal("0") or slt < Decimal("0"):
        raise InvalidCalculationInputError("Valuations cannot be negative.")

    return bv >= slt


def is_take_profit_triggered(
    current_buyback_value: Union[Decimal, float, int, str],
    tp_threshold: Union[Decimal, float, int, str],
) -> bool:
    """
    Check if the Take-Profit barrier is reached.

    Condition:
        CURRENT_BUYBACK_VALUE <= TP_THRESHOLD

    For short straddles, buyback cost declining to or below TP_THRESHOLD
    indicates sufficient theta decay to harvest target profit.
    """
    bv = to_decimal(current_buyback_value, "current_buyback_value")
    tpt = to_decimal(tp_threshold, "tp_threshold")

    if bv < Decimal("0") or tpt < Decimal("0"):
        raise InvalidCalculationInputError("Valuations cannot be negative.")

    return bv <= tpt


# -----------------------------------------------------------------------------
# Comprehensive Straddle Valuation Snapshot & Calculator Class
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class StraddlePremiumSnapshot:
    """
    Immutable, high-precision valuation state snapshot for an active short straddle.
    Encapsulates all monetary values, thresholds, and breach flags in Decimal form.
    """

    call_entry_price: Decimal
    call_executed_quantity: Decimal
    call_entry_premium: Decimal

    put_entry_price: Decimal
    put_executed_quantity: Decimal
    put_entry_premium: Decimal

    total_entry_premium: Decimal
    sl_threshold: Decimal
    tp_threshold: Decimal

    call_ask: Optional[Decimal]
    put_ask: Optional[Decimal]
    call_buyback_cost: Optional[Decimal]
    put_buyback_cost: Optional[Decimal]
    current_buyback_value: Optional[Decimal]

    unrealized_pnl: Optional[Decimal]
    pnl_percent: Optional[Decimal]

    is_sl_triggered: bool
    is_tp_triggered: bool

    settlement_currency: str = "USDT"

    @property
    def total_contracts(self) -> Decimal:
        """Total executed contracts open across both legs."""
        return self.call_executed_quantity + self.put_executed_quantity

    @property
    def is_balanced(self) -> bool:
        """True if Call and Put executed quantities are equal."""
        return self.call_executed_quantity == self.put_executed_quantity


class StraddleCalculator:
    """
    Stateful or operational calculation engine for options straddle strategies.
    Configurable with custom multipliers, SL percentages, and TP percentages.
    """

    def __init__(
        self,
        contract_multiplier: Union[Decimal, float, int, str] = DEFAULT_CONTRACT_MULTIPLIER,
        sl_factor: Union[Decimal, float, int, str] = DEFAULT_SL_FACTOR,
        tp_factor: Union[Decimal, float, int, str] = DEFAULT_TP_FACTOR,
        settlement_currency: str = "USDT",
    ):
        self.contract_multiplier = to_decimal(contract_multiplier, "contract_multiplier")
        self.sl_factor = to_decimal(sl_factor, "sl_factor")
        self.tp_factor = to_decimal(tp_factor, "tp_factor")
        self.settlement_currency = settlement_currency

        if self.contract_multiplier <= Decimal("0"):
            raise InvalidCalculationInputError("Contract multiplier must be positive.")
        if self.sl_factor <= Decimal("0") or self.tp_factor <= Decimal("0"):
            raise InvalidCalculationInputError("Risk factors must be positive.")

    def compute_entry_premium(
        self,
        call_price: Union[Decimal, float, int, str],
        call_qty: Union[Decimal, float, int, str],
        put_price: Union[Decimal, float, int, str],
        put_qty: Union[Decimal, float, int, str],
    ) -> Decimal:
        """Compute gross entry premium collected."""
        return calculate_entry_premium(
            call_entry_price=call_price,
            call_quantity=call_qty,
            put_entry_price=put_price,
            put_quantity=put_qty,
            call_multiplier=self.contract_multiplier,
            put_multiplier=self.contract_multiplier,
        )

    def compute_thresholds(
        self,
        entry_premium: Union[Decimal, float, int, str],
    ) -> Tuple[Decimal, Decimal]:
        """Compute (SL_THRESHOLD, TP_THRESHOLD)."""
        sl = calculate_stop_loss_threshold(entry_premium, self.sl_factor)
        tp = calculate_take_profit_threshold(entry_premium, self.tp_factor)
        return sl, tp

    def compute_buyback(
        self,
        call_ask: Optional[Union[Decimal, float, int, str]],
        call_qty: Union[Decimal, float, int, str],
        put_ask: Optional[Union[Decimal, float, int, str]],
        put_qty: Union[Decimal, float, int, str],
    ) -> Decimal:
        """Compute current live buyback liability using ask prices."""
        return calculate_live_buyback_value(
            call_ask=call_ask,
            call_quantity=call_qty,
            put_ask=put_ask,
            put_quantity=put_qty,
            call_multiplier=self.contract_multiplier,
            put_multiplier=self.contract_multiplier,
        )

    def evaluate(
        self,
        call_entry_price: Union[Decimal, float, int, str],
        call_executed_qty: Union[Decimal, float, int, str],
        put_entry_price: Union[Decimal, float, int, str],
        put_executed_qty: Union[Decimal, float, int, str],
        call_ask: Optional[Union[Decimal, float, int, str]] = None,
        put_ask: Optional[Union[Decimal, float, int, str]] = None,
    ) -> StraddlePremiumSnapshot:
        """
        Evaluate full straddle premium snapshot including entry, thresholds,
        current live buyback, PnL, and barrier trigger states.
        """
        c_px = to_decimal(call_entry_price, "call_entry_price")
        c_qty = to_decimal(call_executed_qty, "call_executed_qty")
        p_px = to_decimal(put_entry_price, "put_entry_price")
        p_qty = to_decimal(put_executed_qty, "put_executed_qty")

        call_prem = calculate_leg_premium(c_px, c_qty, self.contract_multiplier, "call")
        put_prem = calculate_leg_premium(p_px, p_qty, self.contract_multiplier, "put")
        total_entry = call_prem + put_prem

        sl_thresh = calculate_stop_loss_threshold(total_entry, self.sl_factor)
        tp_thresh = calculate_take_profit_threshold(total_entry, self.tp_factor)

        # Compute live buyback if quotes are provided
        c_ask_dec = to_decimal(call_ask, "call_ask", allow_none=True)
        p_ask_dec = to_decimal(put_ask, "put_ask", allow_none=True)

        call_buyback: Optional[Decimal] = None
        put_buyback: Optional[Decimal] = None
        total_buyback: Optional[Decimal] = None
        unrealized_pnl: Optional[Decimal] = None
        pnl_pct: Optional[Decimal] = None
        is_sl = False
        is_tp = False

        if c_ask_dec is not None or p_ask_dec is not None:
            try:
                total_buyback = calculate_live_buyback_value(
                    call_ask=c_ask_dec,
                    call_quantity=c_qty,
                    put_ask=p_ask_dec,
                    put_quantity=p_qty,
                    call_multiplier=self.contract_multiplier,
                    put_multiplier=self.contract_multiplier,
                )
                if c_ask_dec is not None and c_qty > Decimal("0"):
                    call_buyback = (c_ask_dec * c_qty * self.contract_multiplier).quantize(
                        MONEY_DECIMALS, rounding=ROUND_HALF_UP
                    )
                if p_ask_dec is not None and p_qty > Decimal("0"):
                    put_buyback = (p_ask_dec * p_qty * self.contract_multiplier).quantize(
                        MONEY_DECIMALS, rounding=ROUND_HALF_UP
                    )

                unrealized_pnl = calculate_unrealized_pnl(total_entry, total_buyback)
                if total_entry > Decimal("0"):
                    pnl_pct = calculate_pnl_percent(total_entry, total_buyback)
                is_sl = is_stop_loss_triggered(total_buyback, sl_thresh)
                is_tp = is_take_profit_triggered(total_buyback, tp_thresh)
            except MissingQuoteError:
                pass

        return StraddlePremiumSnapshot(
            call_entry_price=c_px,
            call_executed_quantity=c_qty,
            call_entry_premium=call_prem,
            put_entry_price=p_px,
            put_executed_quantity=p_qty,
            put_entry_premium=put_prem,
            total_entry_premium=total_entry,
            sl_threshold=sl_thresh,
            tp_threshold=tp_thresh,
            call_ask=c_ask_dec,
            put_ask=p_ask_dec,
            call_buyback_cost=call_buyback,
            put_buyback_cost=put_buyback,
            current_buyback_value=total_buyback,
            unrealized_pnl=unrealized_pnl,
            pnl_percent=pnl_pct,
            is_sl_triggered=is_sl,
            is_tp_triggered=is_tp,
            settlement_currency=self.settlement_currency,
        )
