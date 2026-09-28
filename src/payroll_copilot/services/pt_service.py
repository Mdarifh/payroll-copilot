"""PT for one employee and period, including half-yearly / annual income bases."""

from decimal import Decimal

from sqlalchemy.orm import Session

from payroll_copilot.calculations.pay_summary import compute_wage_base
from payroll_copilot.calculations.pt import PTInput, PTResult, calculate_pt
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import RuleNotFoundError
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import RuleType, WageBasis
from payroll_copilot.rules.context import build_employee_context
from payroll_copilot.rules.service import RuleService
from payroll_copilot.services.projection import ESTIMATED, PROJECTED, WageProjector
from payroll_copilot.services.salary_service import SalaryCalculationView, SalaryService

ZERO = Decimal(0)


def _basis(basis: WageBasis) -> str:
    return basis.value.lower().replace("_", "-")


def half_year_periods(period: PayrollPeriod) -> list[PayrollPeriod]:
    """The FY half (Apr-Sep or Oct-Mar) containing ``period``."""
    months = period.financial_year.periods()
    return months[:6] if period in months[:6] else months[6:]


def render_pt(result: PTResult, employee_code: str, period: str) -> str:
    label = PayrollPeriod.parse(period).label
    if not result.applicable:
        return f"PT | {employee_code} | {label}: not applicable ({result.reason})"
    if not result.configured:
        return f"PT | {employee_code} | {label}: NOT CALCULATED ({result.reason})"
    status = result.rule.status.value
    lines = [f"PT | {employee_code} | {label} | rule {result.rule.label} [{status}]"]
    if result.reason:
        lines.append(f"  ₹0 - {result.reason}")
    else:
        lines.append(
            f"  {_basis(result.wage_basis)} PT wage {format_inr(result.slab_wage)} | "
            f"slab {result.slab.band} | PT {format_inr(result.amount)}"
        )
    lines.extend(f"  WARNING: {w}" for w in result.warnings)
    return "\n".join(lines)


class PTService:
    def __init__(self, session: Session) -> None:
        self.salary_service = SalaryService(session)
        self.projector = WageProjector(self.salary_service)
        self.rules = RuleService(session)

    def calculate_for_salary(
        self, employee: Employee, period: PayrollPeriod, salary: SalaryCalculationView
    ) -> PTResult:
        if not employee.pt_applicable:
            return PTResult.not_applicable("employee not liable to PT (pt_applicable = false)")
        context = build_employee_context(employee, period)
        try:
            resolved = self.rules.resolve(RuleType.PT, period, context)
        except RuleNotFoundError as exc:
            return PTResult.not_configured(employee.work_state, str(exc))
        rule, warnings = resolved.rule, list(resolved.warnings)

        heads = self.salary_service.salary.heads()
        current = compute_wage_base("PT_WAGE", salary.salary.earnings, heads)
        if rule.wage_basis is WageBasis.MONTHLY:
            slab_wage, breakdown = current.value, dict(current.breakdown)
        else:
            months = (
                half_year_periods(period)
                if rule.wage_basis is WageBasis.HALF_YEARLY
                else period.financial_year.periods()
            )
            rows = self.projector.months(employee, months, period, current.value, "PT_WAGE")
            breakdown = {key: amount for key, (_, amount, _) in rows.items()}
            slab_wage = sum(breakdown.values(), ZERO)
            if any(tag in (ESTIMATED, PROJECTED) for _, _, tag in rows.values()):
                warnings.append(
                    f"{_basis(rule.wage_basis)} PT income uses structure-based estimates for "
                    "months without payroll data"
                )
        return calculate_pt(
            PTInput(
                employee_code=employee.employee_code,
                period=str(period),
                gender=employee.gender,
                slab_wage=slab_wage,
                slab_wage_breakdown=breakdown,
                leaves_this_month=bool(employee.lwd and period.contains(employee.lwd)),
            ),
            rule,
            warnings,
        )

    def calculate(self, employee_code: str, period: PayrollPeriod) -> PTResult:
        salary = self.salary_service.calculate(employee_code, period)
        employee = self.salary_service.employees.get_by_code(employee_code)
        return self.calculate_for_salary(employee, period, salary)
