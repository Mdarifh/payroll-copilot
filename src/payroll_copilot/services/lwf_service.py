"""LWF for one employee and period."""

from sqlalchemy.orm import Session

from payroll_copilot.calculations.lwf import LWFInput, LWFResult, calculate_lwf
from payroll_copilot.calculations.pay_summary import compute_wage_base
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import RuleNotFoundError
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import RuleType
from payroll_copilot.rules.context import build_employee_context
from payroll_copilot.rules.service import RuleService
from payroll_copilot.services.salary_service import SalaryCalculationView, SalaryService


def render_lwf(result: LWFResult, employee_code: str, period: str) -> str:
    label = PayrollPeriod.parse(period).label
    if not result.applicable:
        return f"LWF | {employee_code} | {label}: not applicable ({result.reason})"
    if not result.configured:
        return f"LWF | {employee_code} | {label}: NOT CALCULATED ({result.reason})"
    status = result.rule.status.value
    head = f"LWF | {employee_code} | {label} | rule {result.rule.label} [{status}]"
    if result.reason:
        return f"{head}\n  ₹0 - {result.reason}"
    return (
        f"{head}\n  Employee LWF {format_inr(result.employee_amount)} (deduction) | "
        f"Employer LWF {format_inr(result.employer_amount)} (contribution)"
    )


class LWFService:
    def __init__(self, session: Session) -> None:
        self.salary_service = SalaryService(session)
        self.rules = RuleService(session)

    def calculate_for_salary(
        self, employee: Employee, period: PayrollPeriod, salary: SalaryCalculationView
    ) -> LWFResult:
        if not employee.lwf_applicable:
            return LWFResult.not_applicable("employee not covered by LWF (lwf_applicable = false)")
        context = build_employee_context(employee, period)
        try:
            resolved = self.rules.resolve(RuleType.LWF, period, context)
        except RuleNotFoundError as exc:
            return LWFResult.not_configured(employee.work_state, str(exc))
        heads = self.salary_service.salary.heads()
        wage = compute_wage_base("LWF_WAGE", salary.salary.earnings, heads).value
        return calculate_lwf(
            LWFInput(employee_code=employee.employee_code, period=str(period), lwf_wage=wage),
            resolved.rule,
            resolved.warnings,
        )

    def calculate(self, employee_code: str, period: PayrollPeriod) -> LWFResult:
        salary = self.salary_service.calculate(employee_code, period)
        employee = self.salary_service.employees.get_by_code(employee_code)
        return self.calculate_for_salary(employee, period, salary)
