"""PF for one employee and period, plus comparison across PF rule versions."""

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.pay_summary import compute_wage_base
from payroll_copilot.calculations.pf import PF_COMPONENTS, PFInput, PFResult, calculate_pf
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.models import Employee, PFRule
from payroll_copilot.domain.enums import RuleStatus, RuleType
from payroll_copilot.rules.applicability import applies_to
from payroll_copilot.rules.context import build_employee_context
from payroll_copilot.rules.service import RuleService
from payroll_copilot.services.salary_service import SalaryCalculationView, SalaryService

_LABELS = {
    "EE_PF": "Employee PF", "ER_EPF": "Employer EPF", "EPS": "EPS", "EDLI": "EDLI",
    "EPF_ADMIN": "EPF Admin",
}  # fmt: skip


def render_pf(result: PFResult, employee_code: str, period: str) -> str:
    if not result.applicable:
        return f"PF | {employee_code} | {period}: not applicable ({result.reason})"
    lines = [
        f"PF | {employee_code} | {PayrollPeriod.parse(period).label} | rule {result.rule.label} "
        f"[{result.rule.status.value}]",
        f"  PF wage earned {format_inr(result.pf_wage_actual)} | ceiling "
        f"{format_inr(result.ceiling)} | PF wage {format_inr(result.pf_wage)} | EPS wage "
        f"{format_inr(result.eps_wage)} | EDLI wage {format_inr(result.edli_wage)}",
        "  Employee deduction:",
        f"    {'Employee PF':<14} {format_inr(result.amounts['EE_PF']):>10}",
        "  Employer contributions:",
    ]
    for code in PF_COMPONENTS[1:]:
        lines.append(f"    {_LABELS[code]:<14} {format_inr(result.amounts[code]):>10}")
    lines.extend(f"  WARNING: {w}" for w in result.warnings)
    return "\n".join(lines)


class PFComparisonRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_label: str
    is_applicable_rule: bool
    result: PFResult


class PFService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.salary_service = SalaryService(session)
        self.rules = RuleService(session)

    def pf_input(self, employee: Employee, salary: SalaryCalculationView) -> PFInput:
        heads = self.salary_service.salary.heads()
        # Increment arrears get month-wise PF from the arrear engine; exclude them here.
        wage = compute_wage_base("PF_WAGE", salary.salary.earnings_excluding("ARREAR"), heads)
        return PFInput(
            employee_code=employee.employee_code,
            period=salary.period,
            pf_wage_lines=wage.breakdown,
            paid_days=salary.salary.paid_days,
            basis_days=salary.salary.basis_days,
            wage_restricted=employee.pf_wage_restricted,
            eps_eligible=employee.eps_eligible,
            dob=employee.dob,
        )

    def calculate_for_salary(
        self, employee: Employee, period: PayrollPeriod, salary: SalaryCalculationView
    ) -> PFResult:
        if not employee.pf_applicable:
            return PFResult.not_applicable("employee excluded from PF (pf_applicable = false)")
        if salary.salary.gross_earnings == 0 and not salary.salary.lines:
            return PFResult.not_applicable(f"no earnings in {period}")
        if employee.pf_join_date and employee.pf_join_date > period.end:
            return PFResult.not_applicable(f"PF membership starts {employee.pf_join_date}")
        context = build_employee_context(employee, period)
        resolved = self.rules.resolve(RuleType.PF, period, context)
        return calculate_pf(self.pf_input(employee, salary), resolved.rule, resolved.warnings)

    def calculate(self, employee_code: str, period: PayrollPeriod) -> PFResult:
        salary = self.salary_service.calculate(employee_code, period)
        employee = self.salary_service.employees.get_by_code(employee_code)
        return self.calculate_for_salary(employee, period, salary)

    def compare_rule_versions(
        self, employee_code: str, period: PayrollPeriod
    ) -> list[PFComparisonRow]:
        """Same earnings, every PF rule version the employee qualifies for (dates ignored)."""
        salary = self.salary_service.calculate(employee_code, period)
        employee = self.salary_service.employees.get_by_code(employee_code)
        if not employee.pf_applicable:
            return []
        context = build_employee_context(employee, period)
        applicable = self.rules.resolve(RuleType.PF, period, context).rule
        inp = self.pf_input(employee, salary)
        rows = []
        for rule in self.session.scalars(select(PFRule).order_by(PFRule.effective_from)):
            if rule.status is RuleStatus.RETIRED or not applies_to(rule.applicability, context):
                continue
            result = calculate_pf(inp, rule)
            rows.append(
                PFComparisonRow(
                    rule_label=result.rule.label,
                    is_applicable_rule=rule.id == applicable.id,
                    result=result,
                )
            )
        return rows


def render_comparison(rows: list[PFComparisonRow], employee_code: str, period: str) -> str:
    if not rows:
        return f"PF comparison | {employee_code} | {period}: PF not applicable"
    header = f"  {'Component':<14}" + "".join(
        f"{r.rule_label + (' *' if r.is_applicable_rule else ''):>40}" for r in rows
    )
    lines = [f"PF rule comparison | {employee_code} | {period}  (* = rule for this period)", header]
    fields = [("PF wage", "pf_wage"), ("Ceiling", "ceiling"), ("EPS wage", "eps_wage")]
    for label, attr in fields:
        lines.append(
            f"  {label:<14}" + "".join(f"{format_inr(getattr(r.result, attr)):>40}" for r in rows)
        )
    for code in PF_COMPONENTS:
        cells = "".join(f"{format_inr(r.result.amounts[code]):>40}" for r in rows)
        lines.append(f"  {_LABELS[code]:<14}{cells}")
    if len(rows) >= 2:
        old, new = rows[-2].result.amounts, rows[-1].result.amounts
        diffs = ", ".join(f"{_LABELS[c]} {format_inr(new[c] - old[c])}" for c in PF_COMPONENTS)
        lines.append(f"  Change (latest − previous): {diffs}")
    return "\n".join(lines)
