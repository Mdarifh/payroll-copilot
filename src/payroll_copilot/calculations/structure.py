"""Salary structure engine: split a monthly gross into earning heads per template.

Order of evaluation: FIXED / PERCENT_OF_GROSS / PERCENT_OF_HEAD components are
computed in ``sequence`` order and rounded individually; the single BALANCING
head then takes ``gross - sum(others)`` so rounding never changes the gross.
"""

from collections.abc import Mapping
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from payroll_copilot.calculations.trace import CalculationResult, TraceStep
from payroll_copilot.core.exceptions import StructureError
from payroll_copilot.core.money import RUPEE, RoundingMode, round_money, to_decimal
from payroll_copilot.domain.enums import CalcType, ComparisonStatus, HeadCategory
from payroll_copilot.domain.salary import (
    SalaryAssignmentInfo,
    SalaryHeadInfo,
    StructureComponentSpec,
    StructureTemplate,
)

ZERO = Decimal(0)


def _validate_template(
    template: StructureTemplate, heads: Mapping[str, SalaryHeadInfo]
) -> tuple[list[StructureComponentSpec], StructureComponentSpec | None]:
    label = f"{template.code} v{template.version}"
    specs = sorted(template.components, key=lambda c: c.sequence)
    seen: set[str] = set()
    for spec in specs:
        head = heads.get(spec.head_code)
        if head is None:
            raise StructureError(f"{label}: unknown salary head {spec.head_code}")
        if spec.head_code in seen:
            raise StructureError(f"{label}: head {spec.head_code} appears twice")
        seen.add(spec.head_code)
        if spec.calc_type is not CalcType.STATUTORY and head.category is not HeadCategory.EARNING:
            raise StructureError(
                f"{label}: {spec.head_code} is a {head.category}; templates split gross into "
                "earnings only (statutory heads are computed by their engines)"
            )
        needs_value = spec.calc_type in (
            CalcType.FIXED, CalcType.PERCENT_OF_GROSS, CalcType.PERCENT_OF_HEAD
        )  # fmt: skip
        if needs_value and (spec.value is None or spec.value < 0):
            raise StructureError(f"{label}: {spec.head_code} needs a non-negative value")
        if spec.calc_type is CalcType.PERCENT_OF_HEAD and not spec.base_head_code:
            raise StructureError(f"{label}: {spec.head_code} is PERCENT_OF_HEAD without base head")

    balancing = [s for s in specs if s.calc_type is CalcType.BALANCING]
    if len(balancing) > 1:
        raise StructureError(f"{label}: only one BALANCING head allowed")
    return specs, (balancing[0] if balancing else None)


def compute_structure_breakup(
    template: StructureTemplate,
    monthly_gross: Decimal | int | str,
    heads: Mapping[str, SalaryHeadInfo],
    *,
    rounding_mode: RoundingMode = RoundingMode.HALF_UP,
    precision: Decimal = RUPEE,
    employee_code: str | None = None,
) -> CalculationResult:
    gross = to_decimal(monthly_gross)
    if gross <= 0:
        raise StructureError(f"Monthly gross must be positive, got {gross}")
    specs, balancing = _validate_template(template, heads)

    amounts: dict[str, Decimal] = {}
    steps: list[TraceStep] = []
    warnings: list[str] = []
    for spec in specs:
        name = heads[spec.head_code].name
        match spec.calc_type:
            case CalcType.BALANCING:
                continue
            case CalcType.STATUTORY:
                warnings.append(f"{spec.head_code} is STATUTORY; computed by its own engine")
                continue
            case CalcType.FIXED:
                raw = spec.value
                formula = f"fixed {spec.value}"
                values = {"fixed": spec.value}
            case CalcType.PERCENT_OF_GROSS:
                raw = gross * spec.value / 100
                formula = f"Gross {gross} × {spec.value}%"
                values = {"gross": gross, "rate_percent": spec.value}
            case CalcType.PERCENT_OF_HEAD:
                base = spec.base_head_code
                if base not in amounts:
                    raise StructureError(
                        f"{spec.head_code} depends on {base}, which must be a non-balancing "
                        "head with a lower sequence"
                    )
                raw = amounts[base] * spec.value / 100
                formula = f"{base} {amounts[base]} × {spec.value}%"
                values = {base: amounts[base], "rate_percent": spec.value}
        amount = round_money(raw, rounding_mode, precision)
        amounts[spec.head_code] = amount
        steps.append(
            TraceStep(
                label=name,
                formula=formula,
                values=values,
                result=amount,
                note=None if amount == raw else f"raw {raw} rounded {rounding_mode}",
            )
        )

    allocated = sum(amounts.values(), ZERO)
    if balancing is not None:
        remainder = gross - allocated
        if remainder < 0:
            raise StructureError(
                f"Components before {balancing.head_code} total {allocated}, "
                f"more than gross {gross}"
            )
        amounts[balancing.head_code] = remainder
        steps.append(
            TraceStep(
                label=heads[balancing.head_code].name,
                formula=f"Gross {gross} − other components {allocated}",
                values={"gross": gross, "other_components": allocated},
                result=remainder,
                note="balancing head",
            )
        )
        if remainder == 0:
            warnings.append(f"Balancing head {balancing.head_code} is zero")
    elif allocated != gross:
        raise StructureError(
            f"{template.code} has no BALANCING head and components total {allocated}, "
            f"not gross {gross}"
        )

    order = {s.head_code: s.sequence for s in specs}
    breakdown = dict(sorted(amounts.items(), key=lambda item: order[item[0]]))
    return CalculationResult(
        component="SALARY_STRUCTURE",
        employee_code=employee_code,
        inputs={"structure": f"{template.code} v{template.version}", "monthly_gross": gross},
        formula="Split monthly gross into earning heads per structure template",
        steps=steps,
        raw_value=gross,
        value=sum(breakdown.values(), ZERO),
        rounding_mode=rounding_mode,
        breakdown=breakdown,
        warnings=warnings,
    )


class ComponentCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    head_code: str
    expected: Decimal | None
    actual: Decimal | None
    difference: Decimal | None
    status: ComparisonStatus


class StructureCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    is_consistent: bool
    lines: list[ComponentCheck]
    expected: CalculationResult


def check_assignment_against_template(
    assignment: SalaryAssignmentInfo,
    heads: Mapping[str, SalaryHeadInfo],
    *,
    rounding_mode: RoundingMode = RoundingMode.HALF_UP,
    employee_code: str | None = None,
) -> StructureCheck:
    """Compare stored component amounts with what the template produces."""
    expected = compute_structure_breakup(
        assignment.structure,
        assignment.monthly_gross,
        heads,
        rounding_mode=rounding_mode,
        employee_code=employee_code,
    )
    actual = {c.head_code: c.amount for c in assignment.components}
    lines: list[ComponentCheck] = []
    for head in list(expected.breakdown) + [h for h in actual if h not in expected.breakdown]:
        exp, act = expected.breakdown.get(head), actual.get(head)
        if act is None:
            status, diff = ComparisonStatus.MISSING_ACTUAL, None
        elif exp is None:
            status, diff = ComparisonStatus.MISSING_EXPECTED, None
        else:
            diff = act - exp
            status = ComparisonStatus.MATCH if diff == 0 else ComparisonStatus.MISMATCH
        lines.append(
            ComponentCheck(head_code=head, expected=exp, actual=act, difference=diff, status=status)
        )
    consistent = all(line.status is ComparisonStatus.MATCH for line in lines)
    return StructureCheck(is_consistent=consistent, lines=lines, expected=expected)
