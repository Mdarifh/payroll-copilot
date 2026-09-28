from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Employee, Payroll, PayrollDeduction, PayrollRun
from payroll_copilot.domain.enums import PayrollSource
from payroll_copilot.domain.payslip import ActualPayslip

ZERO = Decimal(0)


def _sum_by_head(lines) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for line in lines:
        totals[line.head_code] = totals.get(line.head_code, ZERO) + line.amount
    return totals


class PayrollRepository:
    """Access to ACTUAL payroll (produced by the payroll system under test)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def run(self, period: PayrollPeriod) -> PayrollRun | None:
        return self.session.scalar(
            select(PayrollRun).where(
                PayrollRun.period == str(period), PayrollRun.source == PayrollSource.ACTUAL
            )
        )

    def deduction_history(
        self, employee_id: int, head_code: str, periods: list[PayrollPeriod]
    ) -> dict[str, Decimal]:
        """{period: amount} of a deduction head in actual payroll, for periods that exist."""
        if not periods:
            return {}
        rows = self.session.execute(
            select(Payroll.period, PayrollDeduction.amount)
            .join(PayrollDeduction, PayrollDeduction.payroll_id == Payroll.id)
            .join(PayrollRun, PayrollRun.id == Payroll.run_id)
            .where(
                Payroll.employee_id == employee_id,
                Payroll.period.in_([str(p) for p in periods]),
                PayrollDeduction.head_code == head_code,
                PayrollRun.source == PayrollSource.ACTUAL,
            )
        )
        history: dict[str, Decimal] = {}
        for period, amount in rows:
            history[period] = history.get(period, ZERO) + amount
        return history

    def actual_payslip(self, employee: Employee, period: PayrollPeriod) -> ActualPayslip | None:
        run = self.run(period)
        if run is None:
            return None
        row = self.session.scalar(
            select(Payroll)
            .where(Payroll.run_id == run.id, Payroll.employee_id == employee.id)
            .options(
                selectinload(Payroll.earnings),
                selectinload(Payroll.deductions),
                selectinload(Payroll.employer_contributions),
            )
        )
        if row is None:
            return None
        return ActualPayslip(
            employee_code=employee.employee_code,
            period=row.period,
            paid_days=row.paid_days,
            lop_days=row.lop_days,
            earnings=_sum_by_head(row.earnings),
            arrear_earnings=sum((e.amount for e in row.earnings if e.is_arrear), ZERO),
            deductions=_sum_by_head(row.deductions),
            employer=_sum_by_head(row.employer_contributions),
            gross_earnings=row.gross_earnings,
            total_deductions=row.total_deductions,
            net_pay=row.net_pay,
            total_employer_contribution=row.total_employer_contribution,
        )

    def employee_ids_in_run(self, period: PayrollPeriod) -> list[int]:
        run = self.run(period)
        if run is None:
            return []
        stmt = select(Payroll.employee_id).where(Payroll.run_id == run.id)
        return list(self.session.scalars(stmt))
