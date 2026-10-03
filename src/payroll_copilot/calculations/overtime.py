"""Overtime: rate per hour and amount (pure functions, no database).

    rate   = employee override                                      (if set)
           = fixed_rate_per_hour                                    (FIXED_RATE policy)
           = Σ rate heads ÷ days_divisor ÷ hours_per_day × multiplier (MULTIPLE_OF_HOURLY)
    amount = hours × rate, rounded half-up to the rupee

The rate is rounded to the paisa before it is multiplied, so the formula shown to the
user ("6 h × ₹250.00/h = ₹1,500") reproduces the amount exactly.
"""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.calculations.trace import CalculationResult, TraceStep
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.core.money import PAISA, RUPEE, RoundingMode, format_inr, round_money

ZERO = Decimal(0)


class OvertimePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    effective_from: date
    enabled: bool = True
    method: Literal["MULTIPLE_OF_HOURLY", "FIXED_RATE"]
    rate_heads: list[str] = Field(default_factory=list)
    days_divisor: Decimal = Decimal(26)
    hours_per_day: Decimal = Decimal(8)
    multiplier: Decimal = Decimal(2)
    fixed_rate_per_hour: Decimal | None = None
    max_hours_per_day: Decimal | None = None
    max_hours_per_month: Decimal | None = None
    eligible_categories: list[str] = Field(default_factory=list)


def overtime_rate(
    policy: OvertimePolicy,
    components: Mapping[str, Decimal],
    *,
    employee_rate: Decimal | None = None,
    employee_code: str | None = None,
    period: str | None = None,
) -> CalculationResult:
    """₹ per overtime hour, with where it came from (``inputs['source']``)."""
    if employee_rate is not None:
        if employee_rate <= 0:
            raise CalculationError("Employee overtime rate must be positive")
        rate = round_money(employee_rate, RoundingMode.HALF_UP, PAISA)
        return CalculationResult(
            component="OT_RATE", employee_code=employee_code, period=period,
            inputs={"source": "Employee configuration (agreed overtime rate)"},
            formula="Agreed overtime rate for this employee",
            steps=[TraceStep(label="Employee overtime rate", result=rate)],
            raw_value=employee_rate, value=rate,
        )  # fmt: skip
    if not policy.enabled:
        raise CalculationError("Overtime is disabled in the company overtime policy")
    source = f"Overtime policy v{policy.version} ({policy.method})"
    if policy.method == "FIXED_RATE":
        if not policy.fixed_rate_per_hour or policy.fixed_rate_per_hour <= 0:
            raise CalculationError("Overtime policy FIXED_RATE has no fixed_rate_per_hour")
        rate = round_money(policy.fixed_rate_per_hour, RoundingMode.HALF_UP, PAISA)
        return CalculationResult(
            component="OT_RATE", employee_code=employee_code, period=period,
            inputs={"source": source}, formula="Fixed overtime rate from company policy",
            steps=[TraceStep(label="Fixed rate per hour", result=rate)],
            raw_value=policy.fixed_rate_per_hour, value=rate,
        )  # fmt: skip

    missing = [h for h in policy.rate_heads if h not in components]
    base = sum((components.get(h, ZERO) for h in policy.rate_heads), ZERO)
    if base <= 0:
        raise CalculationError(
            f"No {' + '.join(policy.rate_heads)} in the salary structure to base overtime on")
    hourly = base / policy.days_divisor / policy.hours_per_day
    raw = hourly * policy.multiplier
    rate = round_money(raw, RoundingMode.HALF_UP, PAISA)
    heads = " + ".join(policy.rate_heads)
    return CalculationResult(
        component="OT_RATE", employee_code=employee_code, period=period,
        inputs={"source": source, **{h: components.get(h, ZERO) for h in policy.rate_heads}},
        formula=f"({heads}) ÷ {policy.days_divisor} days ÷ {policy.hours_per_day} h "
                f"× {policy.multiplier}",
        steps=[
            TraceStep(label=f"Monthly {heads}", result=base),
            TraceStep(label="Ordinary hourly rate",
                      formula=f"{format_inr(base)} ÷ {policy.days_divisor} ÷ "
                              f"{policy.hours_per_day}", result=round_money(
                                  hourly, RoundingMode.HALF_UP, PAISA)),
            TraceStep(label="Overtime rate", formula=f"hourly × {policy.multiplier}",
                      result=rate),
        ],
        raw_value=raw, value=rate,
        warnings=[f"{h} not in the salary structure (taken as 0)" for h in missing],
    )  # fmt: skip


def overtime_amount(hours: Decimal, rate: Decimal) -> tuple[Decimal, str]:
    """(amount, formula) for ``hours`` at ``rate`` per hour."""
    if hours <= 0:
        raise CalculationError("Overtime hours must be positive")
    amount = round_money(hours * rate, RoundingMode.HALF_UP, RUPEE)
    return amount, f"{hours.normalize():f} h × {format_inr(rate)}/h = {format_inr(amount)}"


def check_hours(policy: OvertimePolicy, hours: Decimal, month_hours_so_far: Decimal) -> None:
    """Refuse an entry outside the policy limits (raises CalculationError)."""
    if hours <= 0:
        raise CalculationError("Overtime hours must be positive")
    if policy.max_hours_per_day is not None and hours > policy.max_hours_per_day:
        raise CalculationError(
            f"{hours.normalize():f} h is more than the {policy.max_hours_per_day.normalize():f} h "
            "per day the overtime policy allows")
    total = month_hours_so_far + hours
    if policy.max_hours_per_month is not None and total > policy.max_hours_per_month:
        raise CalculationError(
            f"The month would have {total.normalize():f} h of overtime; the policy allows "
            f"{policy.max_hours_per_month.normalize():f} h")
