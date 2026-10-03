"""Salary arrears.

The chatbot schema has no arrear table yet, so no revision is paid as arrear:
every month is paid at the structure effective in it, and nothing is added later.
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from payroll_copilot.calculations.arrears import ArrearResult
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import PaidDaysBasis
from payroll_copilot.domain.salary import SalaryHeadInfo


class ArrearService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def as_paid_overrides(
        self, employee: Employee, month: PayrollPeriod
    ) -> dict[str, tuple[Decimal, str]]:
        return {}

    def for_payment(
        self, employee: Employee, period: PayrollPeriod, heads: dict[str, SalaryHeadInfo],
        basis: PaidDaysBasis,
    ) -> ArrearResult | None:  # fmt: skip
        return None
