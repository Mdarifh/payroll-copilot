"""Full & Final settlement components (company-policy driven).

    per-day rate       = Σ full-month amount of configured heads / divisor
    leave encashment   = per-day rate (encashment heads) × leave days
    notice shortfall   = max(notice period − days served, 0)
    notice recovery    = per-day rate (notice heads) × shortfall days

The final month salary, statutory deductions and balance TDS come from the
normal payroll calculation of the settlement month; this module only adds the
F&F-specific lines. Gratuity is reported, not computed, until a gratuity rule
is configured.
"""

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.calculations.trace import CalculationResult, TraceStep
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.core.money import RUPEE, RoundingMode, round_money
from payroll_copilot.domain.salary import PayLine, SalaryHeadInfo

ZERO = Decimal(0)
GROSS = "GROSS"


class FnFInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    settlement_period: str
    doj: date
    lwd: date
    full_month_components: list[PayLine]  # last structure before LWD
    leave_encashment_days: Decimal = ZERO
    notice_period_days: int = 0
    notice_days_served: int = 0
    other_recovery: Decimal = ZERO
    encashment_heads: list[str]
    encashment_divisor: Decimal
    notice_heads: list[str]
    notice_divisor: Decimal


class FnFResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    leave_encashment: Decimal
    notice_shortfall_days: int
    notice_recovery: Decimal
    other_recovery: Decimal
    service_years: int
    service_days: int
    gratuity_note: str
    trace: CalculationResult
    warnings: list[str] = Field(default_factory=list)

    def earnings(self) -> list[PayLine]:
        if not self.leave_encashment:
            return []
        return [PayLine(head_code="LEAVE_ENCASH", amount=self.leave_encashment)]

    def deductions(self) -> list[PayLine]:
        lines = []
        if self.notice_recovery:
            lines.append(PayLine(head_code="NOTICE_RECOVERY", amount=self.notice_recovery))
        if self.other_recovery:
            lines.append(PayLine(head_code="OTHER_DED", amount=self.other_recovery))
        return lines


def _base(
    components: Sequence[PayLine], wanted: list[str], heads: Mapping[str, SalaryHeadInfo]
) -> tuple[Decimal, str]:
    if wanted == [GROSS]:
        picked = [c for c in components if heads[c.head_code].part_of_gross]
    else:
        unknown = [h for h in wanted if h not in heads]
        if unknown:
            raise CalculationError(f"Unknown F&F heads in configuration: {unknown}")
        picked = [c for c in components if c.head_code in wanted]
    total = sum((c.amount for c in picked), ZERO)
    return total, " + ".join(f"{c.head_code} {c.amount}" for c in picked) or "none"


def service_length(doj: date, lwd: date) -> tuple[int, int]:
    """(completed years, remaining days) of service, LWD inclusive."""
    years = lwd.year - doj.year - ((lwd.month, lwd.day) < (doj.month, doj.day))
    try:
        anniversary = doj.replace(year=doj.year + years)
    except ValueError:  # 29 February in a non-leap year
        anniversary = doj.replace(year=doj.year + years, day=28)
    return years, (lwd - anniversary).days + 1


def calculate_fnf(
    inp: FnFInput,
    heads: Mapping[str, SalaryHeadInfo],
    rounding_mode: RoundingMode = RoundingMode.HALF_UP,
) -> FnFResult:
    if inp.lwd < inp.doj:
        raise CalculationError("LWD before DOJ")
    if inp.notice_days_served < 0 or inp.notice_period_days < 0:
        raise CalculationError("Notice days cannot be negative")
    steps: list[TraceStep] = []
    warnings = [
        f"Leave encashment rate = ({', '.join(inp.encashment_heads)}) / {inp.encashment_divisor} "
        "and notice recovery rate are company policy settings; verify with HR"
    ]

    enc_base, enc_formula = _base(inp.full_month_components, inp.encashment_heads, heads)
    enc_rate = enc_base / inp.encashment_divisor
    encashment = round_money(enc_rate * inp.leave_encashment_days, rounding_mode, RUPEE)
    steps.append(TraceStep(
        label="Leave encashment",
        formula=f"({enc_formula}) / {inp.encashment_divisor} × {inp.leave_encashment_days} days",
        result=encashment,
    ))  # fmt: skip

    shortfall = max(inp.notice_period_days - inp.notice_days_served, 0)
    notice_base, notice_formula = _base(inp.full_month_components, inp.notice_heads, heads)
    recovery = round_money(notice_base / inp.notice_divisor * shortfall, rounding_mode, RUPEE)
    steps.append(TraceStep(
        label="Notice pay recovery",
        formula=f"({notice_formula}) / {inp.notice_divisor} × shortfall {shortfall} days "
        f"(notice {inp.notice_period_days}, served {inp.notice_days_served})",
        result=recovery,
    ))  # fmt: skip
    if inp.other_recovery:
        steps.append(TraceStep(label="Other recovery", result=inp.other_recovery))

    years, days = service_length(inp.doj, inp.lwd)
    gratuity_note = (
        f"Gratuity NOT CALCULATED: no gratuity rule configured. Service {years} years "
        f"{days} days ({inp.doj} to {inp.lwd}); check eligibility and add a gratuity rule."
    )
    warnings.append(gratuity_note)
    steps.append(TraceStep(label="Service length", formula=f"{years} years {days} days"))

    trace = CalculationResult(
        component="FNF", employee_code=inp.employee_code, period=inp.settlement_period,
        inputs=inp.model_dump(exclude={"full_month_components"}),
        formula="Leave encashment − notice recovery − other recovery (plus final month salary)",
        steps=steps, raw_value=encashment - recovery - inp.other_recovery,
        value=encashment - recovery - inp.other_recovery, rounding_mode=rounding_mode,
        breakdown={"LEAVE_ENCASH": encashment, "NOTICE_RECOVERY": recovery,
                   "OTHER_DED": inp.other_recovery},
        warnings=warnings,
    )  # fmt: skip
    return FnFResult(
        leave_encashment=encashment, notice_shortfall_days=shortfall, notice_recovery=recovery,
        other_recovery=inp.other_recovery, service_years=years, service_days=days,
        gratuity_note=gratuity_note, trace=trace, warnings=warnings,
    )  # fmt: skip
