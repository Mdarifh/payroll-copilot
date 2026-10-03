"""Processed payroll runs (payroll_runs, with the payslip stored as JSON)."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import PayrollRun

ZERO = Decimal(0)


class PayrollRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, employee_id: int, period: PayrollPeriod) -> PayrollRun | None:
        return self.session.scalar(select(PayrollRun).where(
            PayrollRun.employee_id == employee_id, PayrollRun.period == str(period)))  # fmt: skip

    def for_employee(self, employee_id: int, periods: list[PayrollPeriod]) -> list[PayrollRun]:
        return list(self.session.scalars(
            select(PayrollRun)
            .where(PayrollRun.employee_id == employee_id,
                   PayrollRun.period.in_([str(p) for p in periods]))
            .order_by(PayrollRun.period)
        ))  # fmt: skip

    def deduction_history(
        self, employee_id: int, head_code: str, periods: list[PayrollPeriod]
    ) -> dict[str, Decimal]:
        """{period: amount} of a deduction head in processed payroll, for periods that exist."""
        if not periods:
            return {}
        history: dict[str, Decimal] = {}
        for run in self.for_employee(employee_id, periods):
            lines = run.payslip.get("deductions", [])
            history[run.period] = sum(
                (Decimal(line["amount"]) for line in lines if line["head_code"] == head_code), ZERO
            )
        return history
