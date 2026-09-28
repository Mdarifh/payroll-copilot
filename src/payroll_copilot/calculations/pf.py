"""PF engine: Employee EPF, Employer EPF, EPS, EDLI and EPF admin charges.

All rates, ceilings, age limits and rounding come from the resolved PF rule
version; nothing statutory is hard-coded here.

    PF wage (actual) = Σ earned heads with is_pf_wage (incl. arrear lines)
    ceiling          = rule.pf_wage_ceiling  (× paid/basis if rule prorates ceiling)
    PF wage          = min(actual, ceiling) if wage restricted, else actual
    Employee PF      = PF wage × employee_rate
    EPS wage         = min(PF wage, eps ceiling) if EPS eligible, else 0
    EPS              = EPS wage × eps_rate
    Employer EPF     = PF wage × employer_rate − EPS
    EDLI             = min(PF wage, edli ceiling) × edli_rate
    EPF admin        = PF wage × admin_rate
"""

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.calculations.trace import CalculationResult, RuleRef, TraceStep
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.core.money import RUPEE, round_money
from payroll_copilot.domain.enums import CeilingProration, RuleType
from payroll_copilot.domain.salary import PayLine
from payroll_copilot.rules.context import age_on

ZERO = Decimal(0)
PF_COMPONENTS = ("EE_PF", "ER_EPF", "EPS", "EDLI", "EPF_ADMIN")


class PFInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    period: str
    pf_wage_lines: dict[str, Decimal]  # e.g. {"BASIC": 30000, "BASIC (arrear)": 465}
    paid_days: Decimal
    basis_days: Decimal
    wage_restricted: bool = True
    eps_eligible: bool = True
    dob: date


class PFResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    applicable: bool
    reason: str | None = None
    rule: RuleRef | None = None
    pf_wage_actual: Decimal = ZERO
    ceiling: Decimal = ZERO
    pf_wage: Decimal = ZERO
    eps_wage: Decimal = ZERO
    edli_wage: Decimal = ZERO
    amounts: dict[str, Decimal] = Field(default_factory=lambda: dict.fromkeys(PF_COMPONENTS, ZERO))
    employer_total: Decimal = ZERO
    trace: CalculationResult | None = None
    components: dict[str, CalculationResult] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def not_applicable(cls, reason: str) -> "PFResult":
        return cls(applicable=False, reason=reason)

    def deductions(self) -> list[PayLine]:
        return [PayLine(head_code="EE_PF", amount=self.amounts["EE_PF"])] if self.applicable else []

    def employer_contributions(self) -> list[PayLine]:
        if not self.applicable:
            return []
        return [PayLine(head_code=c, amount=self.amounts[c]) for c in PF_COMPONENTS[1:]]


def _step(label: str, formula: str, result: Decimal, **values: Any) -> TraceStep:
    return TraceStep(label=label, formula=formula, values=values, result=result)


def calculate_pf(inp: PFInput, rule: Any, rule_warnings: list[str] | None = None) -> PFResult:
    """Compute all PF components for one employee-month using ``rule`` (a PF rule version)."""
    if inp.basis_days <= 0:
        raise CalculationError("basis_days must be positive")
    period = PayrollPeriod.parse(inp.period)
    mode = rule.rounding_mode
    warnings = list(rule_warnings or [])
    steps: dict[str, TraceStep] = {}

    def rnd(value: Decimal) -> Decimal:
        return round_money(value, mode, RUPEE)

    # 1. PF wage actually earned
    actual = sum(inp.pf_wage_lines.values(), ZERO)
    steps["wage"] = _step(
        "PF wage earned",
        " + ".join(f"{k} {v}" for k, v in inp.pf_wage_lines.items()) or "no PF wage heads",
        actual,
        **inp.pf_wage_lines,
    )
    if any("(arrear)" in k for k in inp.pf_wage_lines):
        warnings.append(
            "Arrear PF wage added to this month's PF wage before the ceiling; "
            "month-wise arrear PF is handled by the arrear engine"
        )

    # 2. Ceiling (optionally prorated)
    def ceiling_for(base: Decimal) -> tuple[Decimal, str]:
        if rule.ceiling_proration is CeilingProration.PAID_DAYS:
            value = base * inp.paid_days / inp.basis_days
            return value, f"{base} × {inp.paid_days} / {inp.basis_days}"
        return base, f"rule ceiling {base}"

    ceiling, ceiling_formula = ceiling_for(rule.pf_wage_ceiling)
    steps["ceiling"] = _step("PF wage ceiling", ceiling_formula, ceiling)

    # 3. PF wage for contribution
    if inp.wage_restricted:
        pf_wage = min(actual, ceiling)
        steps["pf_wage"] = _step("PF wage", f"min({actual}, {ceiling})", pf_wage)
    else:
        pf_wage = actual
        steps["pf_wage"] = _step(
            "PF wage", f"{actual} (not restricted to ceiling: contribution on actual wage)", pf_wage
        )

    # 4. Employee PF
    ee_raw = pf_wage * rule.employee_rate / 100
    ee = rnd(ee_raw)
    steps["ee"] = _step("Employee PF", f"{pf_wage} × {rule.employee_rate}%", ee)

    # 5. EPS
    eps_ceiling, eps_ceiling_formula = ceiling_for(rule.eps_wage_ceiling)
    age = age_on(inp.dob, period.start)
    eps_reason = None
    if not inp.eps_eligible:
        eps_reason = "employee not EPS eligible (eps_eligible = false)"
    elif rule.eps_max_age is not None and age >= rule.eps_max_age:
        eps_reason = f"age {age} ≥ EPS max age {rule.eps_max_age}"
    if eps_reason:
        eps_wage = ZERO
        steps["eps_wage"] = _step("EPS wage", f"0 ({eps_reason})", ZERO)
    else:
        eps_wage = min(pf_wage, eps_ceiling)
        steps["eps_wage"] = _step(
            "EPS wage", f"min({pf_wage}, EPS ceiling {eps_ceiling})", eps_wage,
            eps_ceiling_formula=eps_ceiling_formula,
        )  # fmt: skip
        if rule.eps_max_age is not None and age_on(inp.dob, period.end) >= rule.eps_max_age:
            warnings.append(
                f"Employee reaches age {rule.eps_max_age} during {period}; EPS computed for the "
                "full month (partial-month EPS not modelled)"
            )
    eps = rnd(eps_wage * rule.eps_rate / 100)
    steps["eps"] = _step("EPS", f"{eps_wage} × {rule.eps_rate}%", eps)

    # 6. Employer share
    employer_total = rnd(pf_wage * rule.employer_rate / 100)
    steps["er_total"] = _step(
        "Employer share", f"{pf_wage} × {rule.employer_rate}%", employer_total
    )
    er_epf = employer_total - eps
    steps["er_epf"] = _step("Employer EPF", f"{employer_total} − EPS {eps}", er_epf)

    # 7. EDLI
    edli_ceiling, _ = ceiling_for(rule.edli_wage_ceiling)
    edli_wage = min(pf_wage, edli_ceiling)
    steps["edli_wage"] = _step(
        "EDLI wage", f"min({pf_wage}, EDLI ceiling {edli_ceiling})", edli_wage
    )
    edli = rnd(edli_wage * rule.edli_rate / 100)
    steps["edli"] = _step("EDLI", f"{edli_wage} × {rule.edli_rate}%", edli)

    # 8. Admin charges
    admin = rnd(pf_wage * rule.admin_rate / 100)
    steps["admin"] = _step("EPF admin charges", f"{pf_wage} × {rule.admin_rate}%", admin)
    if rule.admin_min_amount:
        warnings.append(
            f"EPF admin minimum ₹{rule.admin_min_amount} applies per establishment per month, "
            "not per employee"
        )

    ref = RuleRef.from_rule(rule, RuleType.PF)
    inputs = {
        "pf_wage_lines": inp.pf_wage_lines, "paid_days": inp.paid_days,
        "basis_days": inp.basis_days, "wage_restricted": inp.wage_restricted,
        "eps_eligible": inp.eps_eligible, "age": age,
    }  # fmt: skip
    amounts = {"EE_PF": ee, "ER_EPF": er_epf, "EPS": eps, "EDLI": edli, "EPF_ADMIN": admin}
    common = ["wage", "ceiling", "pf_wage"]
    plan = {
        "EE_PF": (common + ["ee"], "PF wage × employee rate", ee_raw),
        "EPS": (common + ["eps_wage", "eps"], "min(PF wage, EPS ceiling) × EPS rate", None),
        "ER_EPF": (common + ["eps_wage", "eps", "er_total", "er_epf"],
                   "PF wage × employer rate − EPS", None),
        "EDLI": (common + ["edli_wage", "edli"], "min(PF wage, EDLI ceiling) × EDLI rate", None),
        "EPF_ADMIN": (common + ["admin"], "PF wage × admin rate", None),
    }  # fmt: skip
    components = {
        code: CalculationResult(
            component=code, employee_code=inp.employee_code, period=inp.period, inputs=inputs,
            rule=ref, formula=formula, steps=[steps[k] for k in keys],
            raw_value=raw if raw is not None else amounts[code], value=amounts[code],
            rounding_mode=mode, warnings=warnings,
        )  # fmt: skip
        for code, (keys, formula, raw) in plan.items()
    }
    trace = CalculationResult(
        component="PF", employee_code=inp.employee_code, period=inp.period, inputs=inputs,
        rule=ref, formula="PF wage = min(earned PF wage, ceiling); contributions at rule rates",
        steps=list(steps.values()), raw_value=ee + employer_total + edli + admin,
        value=ee + employer_total + edli + admin, rounding_mode=mode, breakdown=amounts,
        warnings=warnings,
    )  # fmt: skip
    return PFResult(
        applicable=True, rule=ref, pf_wage_actual=actual, ceiling=ceiling, pf_wage=pf_wage,
        eps_wage=eps_wage, edli_wage=edli_wage, amounts=amounts, employer_total=employer_total,
        trace=trace, components=components, warnings=warnings,
    )  # fmt: skip
