"""The expected payslip for an employee-month (F&F payslip in the settlement month)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Employee, FnFSettlement
from payroll_copilot.services.fnf_service import FnFService
from payroll_copilot.services.payroll_service import PayrollView


class ExpectedPayrollService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.fnf = FnFService(session)
        self.payroll = self.fnf.payroll

    def has_fnf(self, employee: Employee, period: PayrollPeriod) -> bool:
        return bool(
            self.session.scalar(
                select(FnFSettlement.id).where(
                    FnFSettlement.employee_id == employee.id,
                    FnFSettlement.settlement_period == str(period),
                )
            )
        )

    def payslip(self, employee: Employee, period: PayrollPeriod) -> PayrollView:
        if self.has_fnf(employee, period):
            return self.fnf.calculate(employee.employee_code).final_payslip
        return self.payroll.calculate(employee.employee_code, period)
