"""Month-by-month wage figures for multi-month bases (half-yearly PT, annual TDS).

For each month: not employed -> 0; attendance available -> calculated with the
salary engine; otherwise estimated (past) or projected (future) from the salary
structure, prorated by employed calendar days.
"""

from decimal import Decimal

from payroll_copilot.calculations.attendance import employed_span
from payroll_copilot.calculations.pay_summary import compute_wage_base
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PayrollCopilotError
from payroll_copilot.core.money import PAISA, RoundingMode, round_money
from payroll_copilot.db.models import Employee
from payroll_copilot.services.salary_service import SalaryService

ZERO = Decimal(0)
NOT_EMPLOYED = "not employed"
CALCULATED = "calculated"
THIS_MONTH = "this month"
ESTIMATED = "estimated from structure"
PROJECTED = "projected from structure"


class WageProjector:
    def __init__(self, salary_service: SalaryService) -> None:
        self.salary_service = salary_service

    def month_wage(
        self, employee: Employee, month: PayrollPeriod, current: PayrollPeriod, base: str
    ) -> tuple[Decimal, str]:
        span = employed_span(month, employee.doj, employee.lwd)
        if span is None:
            return ZERO, NOT_EMPLOYED
        heads = self.salary_service.salary.heads()
        if month != current and self.salary_service.attendance.get(employee.id, month):
            try:
                salary = self.salary_service.calculate(employee.employee_code, month)
                return compute_wage_base(base, salary.salary.earnings, heads).value, CALCULATED
            except PayrollCopilotError:
                pass  # fall back to the structure estimate
        total = ZERO
        segments, _ = self.salary_service.as_paid_assignments(employee, month, *span)
        for seg in segments:
            full = compute_wage_base(base, seg.components, heads).value
            total += full * seg.active_days / month.calendar_days
        tag = ESTIMATED if month < current else PROJECTED
        return round_money(total, RoundingMode.HALF_UP, PAISA), tag

    def months(
        self,
        employee: Employee,
        months: list[PayrollPeriod],
        current: PayrollPeriod,
        current_value: Decimal,
        base: str,
    ) -> dict[str, tuple[PayrollPeriod, Decimal, str]]:
        """{"<month> (<source>)": (month, amount, source)} for every month given."""
        result = {}
        for month in months:
            if month == current:
                amount, tag = current_value, THIS_MONTH
            else:
                amount, tag = self.month_wage(employee, month, current, base)
            result[f"{month} ({tag})"] = (month, amount, tag)
        return result
