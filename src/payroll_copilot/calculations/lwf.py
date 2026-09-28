"""Labour Welfare Fund engine (state-wise).

Fixed-amount states: employee and employer amounts from the rule.
Rate states:        employee = min(LWF wage × rate, max); employer = employee × multiplier.
Deducted only in the rule's deduction months; optional wage upper limit.
"""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from payroll_copilot.calculations.trace import CalculationResult, RuleRef, TraceStep
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.money import RUPEE, round_money
from payroll_copilot.domain.enums import RuleType
from payroll_copilot.domain.salary import PayLine

ZERO = Decimal(0)


class LWFInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    period: str
    lwf_wage: Decimal


class LWFResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    applicable: bool
    configured: bool = True
    deduction_month: bool = False
    reason: str | None = None
    rule: RuleRef | None = None
    state_code: str | None = None
    lwf_wage: Decimal = ZERO
    employee_amount: Decimal = ZERO
    employer_amount: Decimal = ZERO
    trace: CalculationResult | None = None
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def not_applicable(cls, reason: str) -> "LWFResult":
        return cls(applicable=False, reason=reason)

    @classmethod
    def not_configured(cls, state_code: str, reason: str) -> "LWFResult":
        return cls(applicable=True, configured=False, state_code=state_code, reason=reason)

    @property
    def charged(self) -> bool:
        return self.configured and self.deduction_month and self.reason is None

    def deductions(self) -> list[PayLine]:
        return [PayLine(head_code="LWF_EE", amount=self.employee_amount)] if self.charged else []

    def employer_contributions(self) -> list[PayLine]:
        return [PayLine(head_code="LWF_ER", amount=self.employer_amount)] if self.charged else []


def calculate_lwf(inp: LWFInput, rule: Any, rule_warnings: list[str] | None = None) -> LWFResult:
    period = PayrollPeriod.parse(inp.period)
    ref = RuleRef.from_rule(rule, RuleType.LWF)
    warnings = list(rule_warnings or [])
    steps = [
        TraceStep(
            label="LWF rule",
            formula=f"{rule.state_name}: {rule.frequency.value}, deducted in months "
            f"{rule.deduction_months}",
        )
    ]
    base = dict(applicable=True, rule=ref, state_code=rule.state_code, lwf_wage=inp.lwf_wage)

    def result(ee: Decimal, er: Decimal, reason: str | None, **kw: Any) -> LWFResult:
        trace = CalculationResult(
            component="LWF", employee_code=inp.employee_code, period=inp.period,
            inputs={"lwf_wage": inp.lwf_wage}, rule=ref,
            formula="LWF per state rule in deduction months", steps=steps,
            raw_value=ee + er, value=ee + er, rounding_mode=rule.rounding_mode,
            breakdown={"LWF_EE": ee, "LWF_ER": er}, warnings=warnings,
        )  # fmt: skip
        return LWFResult(**base, employee_amount=ee, employer_amount=er, reason=reason,
                         trace=trace, warnings=warnings, **kw)  # fmt: skip

    if period.month not in rule.deduction_months:
        reason = (
            f"no LWF in {period.label}; {rule.state_code} deducts in months "
            f"{rule.deduction_months}"
        )
        steps.append(TraceStep(label="Deduction month", formula=reason))
        return result(ZERO, ZERO, reason, deduction_month=False)

    if rule.wage_upper_limit is not None and inp.lwf_wage > rule.wage_upper_limit:
        reason = f"LWF wage {inp.lwf_wage} above limit {rule.wage_upper_limit}"
        steps.append(TraceStep(label="Wage limit", formula=reason))
        return result(ZERO, ZERO, reason, deduction_month=True)

    if rule.employee_amount is not None:
        ee, er = rule.employee_amount, rule.employer_amount
        steps.append(TraceStep(label="Employee LWF", formula="fixed amount", result=ee))
        steps.append(TraceStep(label="Employer LWF", formula="fixed amount", result=er))
    else:
        raw = inp.lwf_wage * rule.employee_rate / 100
        maximum = rule.employee_max_amount
        capped = min(raw, maximum) if maximum is not None else raw
        ee = round_money(capped, rule.rounding_mode, RUPEE)
        er = round_money(ee * rule.employer_multiplier, rule.rounding_mode, RUPEE)
        cap = f", max {maximum}" if maximum is not None else ""
        steps.append(
            TraceStep(
                label="Employee LWF",
                formula=f"{inp.lwf_wage} × {rule.employee_rate}% = {raw}{cap}",
                result=ee,
            )
        )
        steps.append(
            TraceStep(label="Employer LWF", formula=f"{ee} × {rule.employer_multiplier}", result=er)
        )
    return result(ee, er, None, deduction_month=True)
