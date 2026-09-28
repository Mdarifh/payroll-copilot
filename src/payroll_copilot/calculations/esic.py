"""ESIC engine: coverage by contribution period, employee and employer shares.

Rates, thresholds, contribution periods and rounding come from the ESIC rule.

Coverage is decided once per contribution period (e.g. Apr-Sep, Oct-Mar) on the
*coverage date* = max(contribution period start, DOJ), using the full-month
ESIC wage rate on that date (arrears excluded). An employee covered at that
point stays covered until the period ends, even if wages rise above the
threshold. Contributions are charged on the ESIC wage actually earned in the
month, including arrears paid in the month.

    ESIC wage  = Σ earned heads with is_esic_wage (incl. arrear lines)
    Employee   = ESIC wage × employee_rate   (rule rounding, typically CEIL)
    Employer   = ESIC wage × employer_rate
"""

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.calculations.trace import CalculationResult, RuleRef, TraceStep
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.core.money import RUPEE, round_money
from payroll_copilot.domain.enums import RuleType
from payroll_copilot.domain.salary import PayLine

ZERO = Decimal(0)
ESIC_COMPONENTS = ("EE_ESIC", "ER_ESIC")


def contribution_period_for(
    period: PayrollPeriod, start_months: list[int]
) -> tuple[date, date]:
    """Contribution period (start, end) containing ``period``."""
    months = sorted(set(start_months))
    if not months:
        raise CalculationError("ESIC rule has no contribution_period_start_months")
    candidates = [m for m in months if m <= period.month]
    if candidates:
        start = PayrollPeriod(period.year, candidates[-1])
    else:
        start = PayrollPeriod(period.year - 1, months[-1])
    # next start month after ``start``
    later = [m for m in months if m > start.month]
    nxt = PayrollPeriod(start.year, later[0]) if later else PayrollPeriod(start.year + 1, months[0])
    return start.start, nxt.previous().end


class ESICInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    period: str
    esic_wage_lines: dict[str, Decimal]  # earned this month, incl. "<HEAD> (arrear)" lines
    coverage_wage: Decimal  # full-month ESIC wage rate on coverage_date (no arrears)
    coverage_date: date
    is_disabled: bool = False


class ESICResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    applicable: bool  # employee is in an ESIC-covered establishment/location
    covered: bool = False  # wage within threshold for this contribution period
    reason: str | None = None
    rule: RuleRef | None = None
    contribution_period: tuple[date, date] | None = None
    coverage_date: date | None = None
    coverage_wage: Decimal = ZERO
    threshold: Decimal = ZERO
    esic_wage: Decimal = ZERO
    amounts: dict[str, Decimal] = Field(
        default_factory=lambda: dict.fromkeys(ESIC_COMPONENTS, ZERO)
    )
    trace: CalculationResult | None = None
    components: dict[str, CalculationResult] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def not_applicable(cls, reason: str) -> "ESICResult":
        return cls(applicable=False, reason=reason)

    def deductions(self) -> list[PayLine]:
        if not self.covered:
            return []
        return [PayLine(head_code="EE_ESIC", amount=self.amounts["EE_ESIC"])]

    def employer_contributions(self) -> list[PayLine]:
        if not self.covered:
            return []
        return [PayLine(head_code="ER_ESIC", amount=self.amounts["ER_ESIC"])]


def _step(label: str, formula: str, result: Decimal | None = None, **values: Any) -> TraceStep:
    return TraceStep(label=label, formula=formula, values=values, result=result)


def calculate_esic(inp: ESICInput, rule: Any, rule_warnings: list[str] | None = None) -> ESICResult:
    period = PayrollPeriod.parse(inp.period)
    warnings = list(rule_warnings or [])
    cp_start, cp_end = contribution_period_for(period, rule.contribution_period_start_months)
    if not cp_start <= inp.coverage_date <= cp_end:
        raise CalculationError(
            f"coverage_date {inp.coverage_date} outside contribution period {cp_start}..{cp_end}"
        )
    ref = RuleRef.from_rule(rule, RuleType.ESIC)

    if inp.is_disabled and rule.disabled_wage_threshold is not None:
        threshold, threshold_note = rule.disabled_wage_threshold, "disabled-employee threshold"
    else:
        threshold, threshold_note = rule.wage_threshold, "wage threshold"

    steps: dict[str, TraceStep] = {
        "period": _step(
            "Contribution period", f"{cp_start} to {cp_end}",
            coverage_date=inp.coverage_date.isoformat(),
        ),
        "coverage": _step(
            "Coverage test",
            f"full-month ESIC wage {inp.coverage_wage} on {inp.coverage_date} "
            f"≤ {threshold_note} {threshold}",
            inp.coverage_wage,
        ),
    }  # fmt: skip
    covered = inp.coverage_wage <= threshold
    inputs = {
        "esic_wage_lines": inp.esic_wage_lines, "coverage_wage": inp.coverage_wage,
        "coverage_date": inp.coverage_date, "is_disabled": inp.is_disabled,
    }  # fmt: skip

    esic_wage = sum(inp.esic_wage_lines.values(), ZERO)
    if not covered:
        reason = (
            f"not covered in contribution period {cp_start}..{cp_end}: full-month ESIC wage "
            f"{inp.coverage_wage} on {inp.coverage_date} exceeds {threshold_note} {threshold}"
        )
        trace = CalculationResult(
            component="ESIC", employee_code=inp.employee_code, period=inp.period, inputs=inputs,
            rule=ref, formula="Coverage decided at start of contribution period",
            steps=list(steps.values()), raw_value=ZERO, value=ZERO, warnings=warnings,
        )  # fmt: skip
        return ESICResult(
            applicable=True, covered=False, reason=reason, rule=ref,
            contribution_period=(cp_start, cp_end), coverage_date=inp.coverage_date,
            coverage_wage=inp.coverage_wage, threshold=threshold, esic_wage=esic_wage,
            trace=trace, warnings=warnings,
        )  # fmt: skip

    regular =sum((v for k, v in inp.esic_wage_lines.items() if "(arrear)" not in k), ZERO)
    if regular > threshold:
        warnings.append(
            f"ESIC wage this month ({regular}) exceeds {threshold}, but the employee stays "
            f"covered until {cp_end} (covered at start of contribution period)"
        )
    if any("(arrear)" in k for k in inp.esic_wage_lines):
        warnings.append("Arrears paid this month are included in ESIC wage; not used for coverage")

    wage_formula = " + ".join(f"{k} {v}" for k, v in inp.esic_wage_lines.items())
    steps["wage"] = TraceStep(
        label="ESIC wage earned",
        formula=wage_formula or "no ESIC wage heads",
        values=dict(inp.esic_wage_lines),
        result=esic_wage,
        note="earned wages include pay for paid holidays and week offs",
    )
    mode = rule.rounding_mode
    ee_raw = esic_wage * rule.employee_rate / 100
    er_raw = esic_wage * rule.employer_rate / 100
    ee, er = round_money(ee_raw, mode, RUPEE), round_money(er_raw, mode, RUPEE)
    steps["ee"] = TraceStep(
        label="Employee ESIC", formula=f"{esic_wage} × {rule.employee_rate}%", result=ee,
        note=None if ee == ee_raw else f"raw {ee_raw}, rounded {mode}",
    )  # fmt: skip
    steps["er"] = TraceStep(
        label="Employer ESIC", formula=f"{esic_wage} × {rule.employer_rate}%", result=er,
        note=None if er == er_raw else f"raw {er_raw}, rounded {mode}",
    )  # fmt: skip
    if esic_wage == 0:
        warnings.append(f"No ESIC wage earned in {period}; contributions are zero")

    amounts = {"EE_ESIC": ee, "ER_ESIC": er}
    common = ["period", "coverage", "wage"]
    components = {
        code: CalculationResult(
            component=code, employee_code=inp.employee_code, period=inp.period, inputs=inputs,
            rule=ref, formula=formula, steps=[steps[k] for k in [*common, key]],
            raw_value=raw, value=amounts[code], rounding_mode=mode, warnings=warnings,
        )  # fmt: skip
        for code, key, formula, raw in (
            ("EE_ESIC", "ee", "ESIC wage × employee rate", ee_raw),
            ("ER_ESIC", "er", "ESIC wage × employer rate", er_raw),
        )
    }
    trace = CalculationResult(
        component="ESIC", employee_code=inp.employee_code, period=inp.period, inputs=inputs,
        rule=ref, formula="Covered at contribution-period start; contribution on earned ESIC wage",
        steps=list(steps.values()), raw_value=ee_raw + er_raw, value=ee + er,
        rounding_mode=mode, breakdown=amounts, warnings=warnings,
    )  # fmt: skip
    return ESICResult(
        applicable=True, covered=True, rule=ref, contribution_period=(cp_start, cp_end),
        coverage_date=inp.coverage_date, coverage_wage=inp.coverage_wage, threshold=threshold,
        esic_wage=esic_wage, amounts=amounts, trace=trace, components=components,
        warnings=warnings,
    )  # fmt: skip
