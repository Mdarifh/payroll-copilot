"""Professional Tax engine (state-wise, slab based).

The resolved PT rule (per work state) defines whether PT is levied, the
frequency, the months in which PT is deducted, the period the slab limits refer
to (monthly / half-yearly / annual income) and gender-specific slabs with
month overrides. The caller supplies the wage for that period (``slab_wage``).

    slab   = the band where lower_exclusive < slab_wage <= upper_inclusive,
             from the employee's gender slab set if one exists, else the default
    amount = slab.month_overrides[month] if present, else slab.amount
             (only in deduction months)
"""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.calculations.trace import CalculationResult, RuleRef, TraceStep
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.core.money import RUPEE, round_money, to_decimal
from payroll_copilot.domain.enums import Frequency, Gender, RuleType, WageBasis
from payroll_copilot.domain.salary import PayLine

ZERO = Decimal(0)


class PTInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    period: str
    gender: Gender
    slab_wage: Decimal
    slab_wage_breakdown: dict[str, Decimal] = Field(default_factory=dict)  # "2026-04 (estimated)"
    leaves_this_month: bool = False


class PTSlabRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    gender: Gender | None
    lower_exclusive: Decimal | None
    upper_inclusive: Decimal | None
    amount: Decimal
    month_overrides: dict[str, Decimal] = Field(default_factory=dict)

    @property
    def band(self) -> str:
        low = self.lower_exclusive if self.lower_exclusive is not None else 0
        high = self.upper_inclusive if self.upper_inclusive is not None else "∞"
        return f"{low} < wage ≤ {high}"


class PTResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    applicable: bool  # employee is liable to PT at all (pt_applicable)
    configured: bool = True  # a PT rule exists for the work state
    levied: bool = False  # state levies PT
    deduction_month: bool = False
    reason: str | None = None
    rule: RuleRef | None = None
    state_code: str | None = None
    wage_basis: WageBasis | None = None
    slab_wage: Decimal = ZERO
    slab: PTSlabRef | None = None
    amount: Decimal = ZERO
    trace: CalculationResult | None = None
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def not_applicable(cls, reason: str) -> "PTResult":
        return cls(applicable=False, reason=reason)

    @classmethod
    def not_configured(cls, state_code: str, reason: str) -> "PTResult":
        return cls(applicable=True, configured=False, state_code=state_code, reason=reason)

    def deductions(self) -> list[PayLine]:
        if not (self.configured and self.levied and self.deduction_month):
            return []
        return [PayLine(head_code="PT", amount=self.amount)]


def select_slab(rule: Any, gender: Gender, wage: Decimal) -> PTSlabRef:
    """Pick the slab for ``wage``; gender-specific set wins over the default set."""
    slabs = list(rule.slabs)
    own = [s for s in slabs if s.gender is gender]
    candidates = own or [s for s in slabs if s.gender is None]
    if not candidates:
        raise CalculationError(f"{rule.rule_code}: no slab set for gender {gender}")
    for slab in sorted(candidates, key=lambda s: s.sequence):
        above_lower = slab.lower_exclusive is None or wage > slab.lower_exclusive
        below_upper = slab.upper_inclusive is None or wage <= slab.upper_inclusive
        if above_lower and below_upper:
            return PTSlabRef(
                gender=slab.gender,
                lower_exclusive=slab.lower_exclusive,
                upper_inclusive=slab.upper_inclusive,
                amount=slab.amount,
                month_overrides={k: to_decimal(v) for k, v in (slab.month_overrides or {}).items()},
            )
    raise CalculationError(f"{rule.rule_code}: no slab covers wage {wage}")


def calculate_pt(inp: PTInput, rule: Any, rule_warnings: list[str] | None = None) -> PTResult:
    period = PayrollPeriod.parse(inp.period)
    ref = RuleRef.from_rule(rule, RuleType.PT)
    warnings = list(rule_warnings or [])
    base = dict(
        applicable=True, rule=ref, state_code=rule.state_code, wage_basis=rule.wage_basis,
        slab_wage=inp.slab_wage,
    )  # fmt: skip
    steps = [
        TraceStep(
            label="PT rule",
            formula=f"{rule.state_name} ({rule.state_code}): {rule.frequency.value}, "
            f"slabs on {rule.wage_basis.value.lower().replace('_', '-')} income, "
            f"deducted in months {rule.deduction_months or '-'}",
        )
    ]

    def result(amount: Decimal, reason: str | None, **kw: Any) -> PTResult:
        trace = CalculationResult(
            component="PT", employee_code=inp.employee_code, period=inp.period,
            inputs={"gender": inp.gender.value, "slab_wage": inp.slab_wage,
                    "slab_wage_breakdown": inp.slab_wage_breakdown},
            rule=ref, formula="PT = slab amount for the state, gender and wage (deduction months)",
            steps=steps, raw_value=amount, value=amount, rounding_mode=rule.rounding_mode,
            warnings=warnings,
        )  # fmt: skip
        return PTResult(**base, amount=amount, reason=reason, trace=trace, warnings=warnings, **kw)

    if not rule.is_levied:
        steps.append(TraceStep(label="Levy", formula=f"PT not levied in {rule.state_name}"))
        return result(ZERO, f"PT not levied in {rule.state_name}", levied=False)

    if period.month not in rule.deduction_months:
        reason = (
            f"no PT deduction in {period.label}; {rule.state_code} deducts in months "
            f"{rule.deduction_months}"
        )
        steps.append(TraceStep(label="Deduction month", formula=reason))
        if inp.leaves_this_month and rule.frequency is not Frequency.MONTHLY:
            warnings.append(
                f"Employee leaves in {period.label}, before the next PT deduction month; "
                "recover PT for the running period in F&F if the state requires it"
            )
        return result(ZERO, reason, levied=True, deduction_month=False)

    wage_formula = " + ".join(f"{k} {v}" for k, v in inp.slab_wage_breakdown.items())
    steps.append(
        TraceStep(
            label=f"PT wage ({rule.wage_basis.value.lower().replace('_', '-')})",
            formula=wage_formula or "earned PT wage",
            values=dict(inp.slab_wage_breakdown),
            result=inp.slab_wage,
        )
    )
    slab = select_slab(rule, inp.gender, inp.slab_wage)
    override = slab.month_overrides.get(str(period.month))
    raw = override if override is not None else slab.amount
    amount = round_money(raw, rule.rounding_mode, RUPEE)
    gender_note = f"gender {slab.gender.value} slabs" if slab.gender else "default slabs"
    steps.append(
        TraceStep(
            label="Slab", formula=f"{slab.band} ({gender_note})", result=slab.amount,
            note=f"{period.label} override {override}" if override is not None else None,
        )  # fmt: skip
    )
    steps.append(TraceStep(label="Professional Tax", formula=f"slab amount for {period.label}",
                           result=amount))  # fmt: skip
    return result(amount, None, levied=True, deduction_month=True, slab=slab)
