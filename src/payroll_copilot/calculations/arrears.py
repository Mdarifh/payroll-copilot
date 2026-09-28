"""Arrear engine: back pay for retrospective salary changes, with month-wise PF impact.

For each arrear month m (the month the revised salary applied to):

    arrear(head, m) = (new − old) × paid days(m) / basis days(m)   (prorated heads)
                    = (new − old)                                    (non-prorated heads)

PF on arrears is computed month-wise with the PF rule and ceiling of month m:

    PF arrear(m) = PF(new salary of m) − PF(old salary of m)

so an arrear for August 2026 uses the ₹15,000-ceiling rule even when it is paid
in September 2026. ESIC, PT and TDS treat arrears as wages of the payment month
(handled by their engines, which receive the arrear earning lines).
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.calculations.pay_summary import compute_wage_base
from payroll_copilot.calculations.pf import PF_COMPONENTS, PFInput, calculate_pf
from payroll_copilot.calculations.salary import EarningLine
from payroll_copilot.calculations.trace import CalculationResult, TraceStep
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.core.money import RUPEE, RoundingMode, round_money
from payroll_copilot.domain.enums import ComparisonStatus, HeadCategory
from payroll_copilot.domain.salary import PayLine, SalaryHeadInfo

ZERO = Decimal(0)


class ArrearChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    head_code: str
    old_monthly_amount: Decimal
    new_monthly_amount: Decimal


class ArrearMonthInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: str
    paid_days: Decimal
    basis_days: Decimal
    days_source: str  # "attendance" or "arrear_days"
    changes: list[ArrearChange]
    new_components: list[PayLine]  # full-month revised structure for the month


class ArrearMonthResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: str
    paid_days: Decimal
    basis_days: Decimal
    lines: list[EarningLine]
    pf_rule: str | None = None
    pf_old: dict[str, Decimal] = Field(default_factory=dict)
    pf_new: dict[str, Decimal] = Field(default_factory=dict)
    pf_impact: dict[str, Decimal] = Field(default_factory=dict)
    pf_note: str | None = None


class ArrearComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    head_code: str
    expected: Decimal
    uploaded: Decimal | None
    difference: Decimal | None
    status: ComparisonStatus


class ArrearResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    paid_in_period: str
    months: list[ArrearMonthResult]
    total: Decimal
    pf_impact: dict[str, Decimal]
    comparisons: list[ArrearComparison]
    trace: CalculationResult
    warnings: list[str] = Field(default_factory=list)

    @property
    def lines(self) -> list[EarningLine]:
        return [line for month in self.months for line in month.lines]

    def deductions(self) -> list[PayLine]:
        amount = self.pf_impact.get("EE_PF", ZERO)
        return [PayLine(head_code="EE_PF", amount=amount, is_arrear=True)] if amount else []

    def employer_contributions(self) -> list[PayLine]:
        return [
            PayLine(head_code=code, amount=self.pf_impact[code], is_arrear=True)
            for code in PF_COMPONENTS[1:]
            if self.pf_impact.get(code)
        ]


def _earned(components: Sequence[PayLine], paid: Decimal, basis: Decimal,
            heads: Mapping[str, SalaryHeadInfo], mode: RoundingMode) -> list[PayLine]:  # fmt: skip
    earned = []
    for comp in components:
        head = heads[comp.head_code]
        raw = comp.amount * paid / basis if head.is_prorated else comp.amount
        earned.append(PayLine(head_code=comp.head_code, amount=round_money(raw, mode, RUPEE)))
    return earned


def arrear_lines_for_month(
    month: ArrearMonthInput,
    heads: Mapping[str, SalaryHeadInfo],
    rounding_mode: RoundingMode = RoundingMode.HALF_UP,
) -> list[EarningLine]:
    if month.basis_days <= 0:
        raise CalculationError(f"{month.period}: basis days must be positive")
    lines = []
    for change in month.changes:
        head = heads.get(change.head_code)
        if head is None or head.category is not HeadCategory.EARNING:
            raise CalculationError(f"Arrear head {change.head_code} is not an earning head")
        diff = change.new_monthly_amount - change.old_monthly_amount
        if head.is_prorated:
            raw = diff * month.paid_days / month.basis_days
            formula = (
                f"({change.new_monthly_amount} − {change.old_monthly_amount}) × "
                f"{month.paid_days} / {month.basis_days}"
            )
        else:
            raw, formula = diff, f"{change.new_monthly_amount} − {change.old_monthly_amount}"
        lines.append(
            EarningLine(
                head_code=head.code, name=head.name, source=f"Arrear {month.period}",
                full_month_amount=diff,
                paid_days=month.paid_days if head.is_prorated else None,
                basis_days=month.basis_days if head.is_prorated else None,
                is_prorated=head.is_prorated, raw_amount=raw,
                amount=round_money(raw, rounding_mode, RUPEE), is_arrear=True, kind="ARREAR",
                formula=formula,
            )  # fmt: skip
        )
    return lines


def pf_arrear_impact(
    month: ArrearMonthInput,
    heads: Mapping[str, SalaryHeadInfo],
    rule: Any,
    pf_input: dict[str, Any],
) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, Decimal]]:
    """(old PF, new PF, new − old) for one arrear month under that month's PF rule."""
    changes = {c.head_code: c.old_monthly_amount for c in month.changes}
    old_components = [
        PayLine(head_code=c.head_code, amount=changes.get(c.head_code, c.amount))
        for c in month.new_components
    ]
    mode = rule.rounding_mode
    results = []
    for components in (old_components, month.new_components):
        earned = _earned(components, month.paid_days, month.basis_days, heads, mode)
        wage = compute_wage_base("PF_WAGE", earned, heads)
        inp = PFInput(
            period=month.period, pf_wage_lines=wage.breakdown, paid_days=month.paid_days,
            basis_days=month.basis_days, **pf_input,
        )  # fmt: skip
        results.append(calculate_pf(inp, rule).amounts)
    old, new = results
    return old, new, {code: new[code] - old[code] for code in PF_COMPONENTS}


def summarize_arrears(
    employee_code: str,
    paid_in_period: str,
    months: list[ArrearMonthResult],
    uploaded: Mapping[str, Decimal],
    warnings: list[str],
) -> ArrearResult:
    steps: list[TraceStep] = []
    expected: dict[str, Decimal] = {}
    for month in months:
        steps.append(
            TraceStep(
                label=f"Arrear month {month.period}",
                formula=f"paid {month.paid_days} of {month.basis_days} days",
                result=month.paid_days, unit="DAYS",
            )  # fmt: skip
        )
        for line in month.lines:
            expected[line.head_code] = expected.get(line.head_code, ZERO) + line.amount
            steps.append(TraceStep(label=f"{line.name} arrear {month.period}",
                                   formula=line.formula, result=line.amount))  # fmt: skip
        if month.pf_rule:
            impact = ", ".join(f"{k} {v}" for k, v in month.pf_impact.items())
            steps.append(TraceStep(
                label=f"PF on arrear {month.period} ({month.pf_rule})",
                formula=f"PF(new) − PF(old): {impact}",
                result=month.pf_impact.get("EE_PF", ZERO),
                note=month.pf_note,
            ))  # fmt: skip
    total = sum(expected.values(), ZERO)
    pf_total = {
        code: sum((m.pf_impact.get(code, ZERO) for m in months), ZERO) for code in PF_COMPONENTS
    }

    comparisons = []
    for head in sorted(set(expected) | set(uploaded)):
        exp, up = expected.get(head, ZERO), uploaded.get(head)
        if up is None:
            status, diff = ComparisonStatus.MISSING_ACTUAL, None
        else:
            diff = up - exp
            status = ComparisonStatus.MATCH if diff == 0 else ComparisonStatus.MISMATCH
        comparisons.append(ArrearComparison(head_code=head, expected=exp, uploaded=up,
                                            difference=diff, status=status))  # fmt: skip
        if status is ComparisonStatus.MISMATCH:
            warnings.append(f"Uploaded arrear for {head} is {up}, expected {exp} (diff {diff})")

    trace = CalculationResult(
        component="ARREARS", employee_code=employee_code, period=paid_in_period,
        inputs={"months": [m.period for m in months], "uploaded": dict(uploaded)},
        formula="Σ (new − old) × paid days / basis days per arrear month; PF month-wise",
        steps=steps, raw_value=total, value=total,
        breakdown=expected, warnings=warnings,
    )  # fmt: skip
    return ArrearResult(
        employee_code=employee_code, paid_in_period=paid_in_period, months=months, total=total,
        pf_impact=pf_total, comparisons=comparisons, trace=trace, warnings=warnings,
    )  # fmt: skip
