"""Group pay lines into Earnings / Employee Deductions / Employer Contributions.

Totals:
    Gross earnings      = Σ earnings flagged part_of_gross
    Employee deductions = Σ deductions
    Net pay             = Gross earnings − Employee deductions
    Employer contrib.   = Σ employer contributions (NOT deducted from net pay)
    Monthly CTC         = Σ earnings flagged part_of_ctc + Σ employer contributions
                          flagged part_of_ctc
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal

from payroll_copilot.calculations.trace import CalculationResult, TraceStep
from payroll_copilot.core.exceptions import StructureError
from payroll_copilot.domain.enums import HeadCategory
from payroll_copilot.domain.salary import BucketLine, PayLine, PaySummary, SalaryHeadInfo

ZERO = Decimal(0)

# Wage base name -> salary head flag that includes a head in that base.
WAGE_BASE_FLAGS: dict[str, str] = {
    "PF_WAGE": "is_pf_wage",
    "ESIC_WAGE": "is_esic_wage",
    "PT_WAGE": "is_pt_wage",
    "LWF_WAGE": "is_lwf_wage",
    "TAXABLE_EARNINGS": "is_taxable",
}


def _bucket(
    lines: Sequence[PayLine],
    category: HeadCategory,
    heads: Mapping[str, SalaryHeadInfo],
) -> list[BucketLine]:
    result = []
    for line in lines:
        head = heads.get(line.head_code)
        if head is None:
            raise StructureError(f"Unknown salary head {line.head_code}")
        if head.category is not category:
            raise StructureError(
                f"{line.head_code} is a {head.category}, not a {category}; "
                "employee deductions and employer contributions must not be mixed"
            )
        if line.amount < 0 and not line.is_arrear:
            raise StructureError(f"{line.head_code}: negative amount {line.amount} (not an arrear)")
        result.append(
            BucketLine(
                head_code=line.head_code,
                name=head.name,
                amount=line.amount,
                is_arrear=line.is_arrear,
            )
        )
    return sorted(result, key=lambda b: (heads[b.head_code].display_order, b.is_arrear))


def compute_wage_base(
    base: str,
    earnings: Sequence[PayLine],
    heads: Mapping[str, SalaryHeadInfo],
    *,
    employee_code: str | None = None,
    period: str | None = None,
) -> CalculationResult:
    """Sum of earning heads whose flag marks them as part of ``base`` (e.g. PF_WAGE)."""
    if base not in WAGE_BASE_FLAGS:
        raise StructureError(f"Unknown wage base {base}; known: {sorted(WAGE_BASE_FLAGS)}")
    flag = WAGE_BASE_FLAGS[base]
    included: dict[str, Decimal] = {}
    excluded: list[str] = []
    for line in earnings:
        head = heads[line.head_code]
        key = f"{line.head_code} (arrear)" if line.is_arrear else line.head_code
        if getattr(head, flag):
            included[key] = included.get(key, ZERO) + line.amount
        else:
            excluded.append(key)
    total = sum(included.values(), ZERO)
    steps = [
        TraceStep(
            label=f"Heads included in {base}",
            formula=" + ".join(f"{k} {v}" for k, v in included.items()) or "none",
            values=dict(included),
            result=total,
        )
    ]
    if excluded:
        steps.append(
            TraceStep(label=f"Heads excluded from {base}", values={"heads": excluded}, note=flag)
        )
    return CalculationResult(
        component=base,
        employee_code=employee_code,
        period=period,
        inputs={"earnings": {line.head_code: line.amount for line in earnings}},
        formula=f"Σ earnings where {flag} = true",
        steps=steps,
        raw_value=total,
        value=total,
        breakdown=included,
    )


def summarize_pay(
    earnings: Sequence[PayLine],
    deductions: Sequence[PayLine],
    employer_contributions: Sequence[PayLine],
    heads: Mapping[str, SalaryHeadInfo],
) -> PaySummary:
    earning_lines = _bucket(earnings, HeadCategory.EARNING, heads)
    deduction_lines = _bucket(deductions, HeadCategory.DEDUCTION, heads)
    employer_lines = _bucket(employer_contributions, HeadCategory.EMPLOYER_CONTRIBUTION, heads)

    warnings: list[str] = []
    gross = sum((b.amount for b in earning_lines if heads[b.head_code].part_of_gross), ZERO)
    not_in_gross = [b.head_code for b in earning_lines if not heads[b.head_code].part_of_gross]
    if not_in_gross:
        warnings.append(f"Earnings not part of gross: {', '.join(not_in_gross)}")

    total_deductions = sum((b.amount for b in deduction_lines), ZERO)
    total_employer = sum((b.amount for b in employer_lines), ZERO)
    net = gross - total_deductions
    if net < 0:
        warnings.append(f"Net pay is negative ({net}); deductions exceed gross earnings")

    ctc = sum(
        (b.amount for b in earning_lines + employer_lines if heads[b.head_code].part_of_ctc), ZERO
    )
    wage_bases = {base: compute_wage_base(base, earnings, heads).value for base in WAGE_BASE_FLAGS}
    return PaySummary(
        earnings=earning_lines,
        deductions=deduction_lines,
        employer_contributions=employer_lines,
        gross_earnings=gross,
        total_deductions=total_deductions,
        net_pay=net,
        total_employer_contributions=total_employer,
        monthly_ctc=ctc,
        wage_bases=wage_bases,
        warnings=warnings,
    )
