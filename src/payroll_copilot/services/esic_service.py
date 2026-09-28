"""ESIC for one employee and period."""

from sqlalchemy.orm import Session

from payroll_copilot.calculations.esic import (
    ESICInput,
    ESICResult,
    calculate_esic,
    contribution_period_for,
)
from payroll_copilot.calculations.pay_summary import compute_wage_base
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import RuleType
from payroll_copilot.rules.context import build_employee_context
from payroll_copilot.rules.service import RuleService
from payroll_copilot.services.salary_service import SalaryCalculationView, SalaryService


def render_esic(result: ESICResult, employee_code: str, period: str) -> str:
    label = PayrollPeriod.parse(period).label
    if not result.applicable:
        return f"ESIC | {employee_code} | {label}: not applicable ({result.reason})"
    cp = result.contribution_period
    lines = [
        f"ESIC | {employee_code} | {label} | rule {result.rule.label} [{result.rule.status.value}]",
        f"  Contribution period {cp[0]} to {cp[1]} | coverage tested on {result.coverage_date}: "
        f"{format_inr(result.coverage_wage)} vs threshold {format_inr(result.threshold)} -> "
        + ("COVERED" if result.covered else "NOT COVERED"),
    ]
    if result.covered:
        lines += [
            f"  ESIC wage earned {format_inr(result.esic_wage)}",
            f"  Employee ESIC (deduction)   {format_inr(result.amounts['EE_ESIC']):>10}",
            f"  Employer ESIC (contribution){format_inr(result.amounts['ER_ESIC']):>10}",
        ]
    else:
        lines.append(f"  {result.reason}")
    lines.extend(f"  WARNING: {w}" for w in result.warnings)
    return "\n".join(lines)


class ESICService:
    def __init__(self, session: Session) -> None:
        self.salary_service = SalaryService(session)
        self.rules = RuleService(session)

    def calculate_for_salary(
        self, employee: Employee, period: PayrollPeriod, salary: SalaryCalculationView
    ) -> ESICResult:
        if not employee.esic_applicable:
            return ESICResult.not_applicable(
                "employee not in an ESIC-covered establishment/location (esic_applicable = false)"
            )
        if not salary.salary.lines:
            return ESICResult.not_applicable(f"no earnings in {period}")
        context = build_employee_context(employee, period)
        resolved = self.rules.resolve(RuleType.ESIC, period, context)
        rule = resolved.rule
        warnings = list(resolved.warnings)

        heads = self.salary_service.salary.heads()
        cp_start, _ = contribution_period_for(period, rule.contribution_period_start_months)
        coverage_date = max(cp_start, employee.doj)
        assignments = self.salary_service.salary.assignments_between(
            employee.id, coverage_date, coverage_date
        )
        if not assignments:
            assignments = self.salary_service.salary.assignments_between(
                employee.id, coverage_date, period.end
            )[:1]
            if assignments:
                coverage_date = assignments[0].effective_from
                warnings.append(
                    f"No salary structure at contribution-period start; coverage tested on "
                    f"{coverage_date} (first structure in the period)"
                )
        coverage_wage = (
            compute_wage_base("ESIC_WAGE", assignments[-1].components, heads).value
            if assignments
            else salary.salary.full_month_gross
        )
        earned = compute_wage_base("ESIC_WAGE", salary.salary.earnings, heads)
        return calculate_esic(
            ESICInput(
                employee_code=employee.employee_code,
                period=str(period),
                esic_wage_lines=earned.breakdown,
                coverage_wage=coverage_wage,
                coverage_date=coverage_date,
                is_disabled=employee.is_disabled,
            ),
            rule,
            warnings,
        )

    def calculate(self, employee_code: str, period: PayrollPeriod) -> ESICResult:
        salary = self.salary_service.calculate(employee_code, period)
        employee = self.salary_service.employees.get_by_code(employee_code)
        return self.calculate_for_salary(employee, period, salary)
