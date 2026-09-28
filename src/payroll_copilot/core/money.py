"""Money helpers.

All payroll amounts are ``Decimal``. Floats are rejected on purpose: a float such
as 0.1 cannot be represented exactly and would silently corrupt payroll values.
Rates are stored as percentages (``Decimal("12")`` means 12%) because that is
how rules are written and it keeps calculation traces readable.
"""

from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
    InvalidOperation,
)
from enum import StrEnum

from payroll_copilot.core.exceptions import MoneyError

RUPEE = Decimal("1")
PAISA = Decimal("0.01")
HUNDRED = Decimal("100")


class RoundingMode(StrEnum):
    HALF_UP = "HALF_UP"  # 0.5 goes up (common payroll convention)
    HALF_EVEN = "HALF_EVEN"  # banker's rounding
    CEIL = "CEIL"  # always towards +infinity (e.g. "next higher rupee")
    FLOOR = "FLOOR"  # always towards -infinity
    NONE = "NONE"  # keep exact value


_DECIMAL_ROUNDING = {
    RoundingMode.HALF_UP: ROUND_HALF_UP,
    RoundingMode.HALF_EVEN: ROUND_HALF_EVEN,
    RoundingMode.CEIL: ROUND_CEILING,
    RoundingMode.FLOOR: ROUND_FLOOR,
}


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Convert an int/str/Decimal to Decimal. Floats and bools are rejected."""
    if isinstance(value, bool | float):
        raise MoneyError(
            f"{type(value).__name__} value {value!r} not allowed for money; use str or Decimal"
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int | str):
        try:
            result = Decimal(str(value).strip().replace(",", ""))
        except InvalidOperation as exc:
            raise MoneyError(f"Invalid money value: {value!r}") from exc
    else:
        raise MoneyError(f"Unsupported money type: {type(value).__name__}")
    if not result.is_finite():
        raise MoneyError(f"Money value must be finite: {value!r}")
    return result


def round_money(
    value: Decimal | int | str,
    mode: RoundingMode | str = RoundingMode.HALF_UP,
    precision: Decimal = RUPEE,
) -> Decimal:
    """Round ``value`` to ``precision`` (RUPEE or PAISA) using ``mode``."""
    amount = to_decimal(value)
    mode = RoundingMode(mode)
    if mode is RoundingMode.NONE:
        return amount
    return amount.quantize(precision, rounding=_DECIMAL_ROUNDING[mode])


def apply_rate(amount: Decimal | int | str, rate_percent: Decimal | int | str) -> Decimal:
    """Return ``amount × rate_percent / 100`` without rounding."""
    return to_decimal(amount) * to_decimal(rate_percent) / HUNDRED


def format_inr(value: Decimal | int | str, with_symbol: bool = True) -> str:
    """Format using Indian digit grouping, e.g. 1234567.5 -> '₹12,34,567.50'.

    Whole-rupee amounts are shown without paise.
    """
    amount = to_decimal(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount).quantize(PAISA, rounding=ROUND_HALF_UP)
    rupees, paise = f"{amount:.2f}".split(".")

    if len(rupees) > 3:
        head, tail = rupees[:-3], rupees[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        rupees = ",".join(groups) + "," + tail

    text = rupees if paise == "00" else f"{rupees}.{paise}"
    return f"{sign}{'₹' if with_symbol else ''}{text}"
