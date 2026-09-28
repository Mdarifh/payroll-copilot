"""Full & Final settlement for a separated employee."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.fnf import FnFInput, FnFResult, calculate_fnf
from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import CalculationError, NotFoundError
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.models import FnFSettlement
from payroll_copilot.services.payroll_service import PayrollCalculationService, PayrollView


class FnFView(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    fnf: FnFResult
    final_payslip: PayrollView

    @property
    def net_settlement(self) -> Decimal:
        return self.final_payslip.summary.net_pay

    def to_text(self) -> str:
        f = self.fnf
        lines = [
            f"Full & Final | {self.employee_code} | settlement {self.final_payslip.period}",
            f"  Leave encashment        {format_inr(f.leave_encashment):>12}",
            f"  Notice recovery         {format_inr(-f.notice_recovery):>12} "
            f"(shortfall {f.notice_shortfall_days} days)",
        ]
        if f.other_recovery:
            lines.append(f"  Other recovery          {format_inr(-f.other_recovery):>12}")
        lines += [f"  WARNING: {w}" for w in f.warnings]
        lines += [
            "",
            self.final_payslip.to_text(),
            "",
            f"NET F&F SETTLEMENT: {format_inr(self.net_settlement)}",
        ]
        return "\n".join(lines)


class FnFService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.payroll = PayrollCalculationService(session)
        self.salary_service = self.payroll.salary_service

    def calculate(self, employee_code: str) -> FnFView:
        employee = self.salary_service.employees.get_by_code(employee_code)
        settlement = self.session.scalar(
            select(FnFSettlement).where(FnFSettlement.employee_id == employee.id)
        )
        if settlement is None:
            raise NotFoundError(f"No F&F settlement for {employee.employee_code}")
        if employee.lwd is None or employee.lwd != settlement.lwd:
            raise CalculationError(
                f"F&F LWD {settlement.lwd} does not match employee LWD {employee.lwd}"
            )
        period = PayrollPeriod.parse(settlement.settlement_period)
        assignments = self.salary_service.salary.assignments_between(
            employee.id, settlement.lwd, settlement.lwd
        )
        if not assignments:
            raise NotFoundError(f"No salary structure on LWD {settlement.lwd}")

        settings = get_settings()
        fnf = calculate_fnf(
            FnFInput(
                employee_code=employee.employee_code,
                settlement_period=str(period),
                doj=employee.doj,
                lwd=settlement.lwd,
                full_month_components=assignments[-1].components,
                leave_encashment_days=settlement.leave_encashment_days,
                notice_period_days=settlement.notice_period_days,
                notice_days_served=settlement.notice_days_served,
                other_recovery=settlement.other_recovery,
                encashment_heads=settings.fnf_leave_encashment_heads,
                encashment_divisor=settings.fnf_leave_encashment_divisor,
                notice_heads=settings.fnf_notice_recovery_heads,
                notice_divisor=settings.fnf_notice_recovery_divisor,
            ),
            self.salary_service.salary.heads(),
            settings.default_rounding_mode,
        )
        payslip = self.payroll.calculate(
            employee.employee_code,
            period,
            variable_earnings=fnf.earnings(),
            extra_deductions=fnf.deductions(),
        )
        return FnFView(employee_code=employee.employee_code, fnf=fnf, final_payslip=payslip)
