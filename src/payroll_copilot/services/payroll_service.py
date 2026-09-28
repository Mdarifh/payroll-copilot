"""Payroll for one employee-month: earnings + statutory components → net pay and CTC.

Statutory engines are added phase by phase; ``pending_components`` lists the
ones not calculated yet so a payslip is never mistaken for complete.
"""

from collections.abc import Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from payroll_copilot.calculations.esic import ESICResult
from payroll_copilot.calculations.lwf import LWFResult
from payroll_copilot.calculations.pay_summary import summarize_pay
from payroll_copilot.calculations.pf import PFResult
from payroll_copilot.calculations.pt import PTResult
from payroll_copilot.calculations.tds import TDSResult
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.money import format_inr
from payroll_copilot.domain.salary import PayLine, PaySummary
from payroll_copilot.services.esic_service import ESICService
from payroll_copilot.services.lwf_service import LWFService
from payroll_copilot.services.pf_service import PFService
from payroll_copilot.services.pt_service import PTService
from payroll_copilot.services.salary_service import SalaryCalculationView
from payroll_copilot.services.tds_service import TDSService

PENDING_COMPONENTS: list[str] = []  # every statutory engine is wired in


class PayrollView(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    employee_name: str
    period: str
    salary: SalaryCalculationView
    pf: PFResult
    esic: ESICResult
    pt: PTResult
    lwf: LWFResult
    tds: TDSResult
    summary: PaySummary
    pending_components: list[str]
    warnings: list[str] = Field(default_factory=list)

    def to_text(self) -> str:
        s = self.summary
        lines = [
            f"Payslip (expected) | {self.employee_code} ({self.employee_name}) | "
            f"{PayrollPeriod.parse(self.period).label}",
            f"Paid days {self.salary.salary.paid_days} of {self.salary.salary.basis_days}",
            "",
            "EARNINGS",
        ]
        for b in s.earnings:
            name = b.name + (" (arrear)" if b.is_arrear else "")
            lines.append(f"  {name:<30} {format_inr(b.amount):>12}")
        lines.append(f"  {'Gross earnings':<30} {format_inr(s.gross_earnings):>12}")
        lines.append("EMPLOYEE DEDUCTIONS")
        for b in s.deductions:
            name = b.name + (" (arrear)" if b.is_arrear else "")
            lines.append(f"  {name:<30} {format_inr(b.amount):>12}")
        lines.append(f"  {'Total deductions':<30} {format_inr(s.total_deductions):>12}")
        lines.append(f"NET PAY{'':<25} {format_inr(s.net_pay):>12}")
        lines.append("")
        lines.append("EMPLOYER CONTRIBUTIONS (not deducted from net pay)")
        for b in s.employer_contributions:
            name = b.name + (" (arrear)" if b.is_arrear else "")
            lines.append(f"  {name:<30} {format_inr(b.amount):>12}")
        total_er = format_inr(s.total_employer_contributions)
        lines.append(f"  {'Total employer contributions':<30} {total_er:>12}")
        lines.append(f"MONTHLY CTC{'':<21} {format_inr(s.monthly_ctc):>12}")
        lines.append("")
        for name, result in (("PF", self.pf), ("ESIC", self.esic)):
            if not result.applicable or not getattr(result, "covered", True):
                lines.append(f"{name}: not deducted - {result.reason}")
        for name, result in (("PT", self.pt), ("LWF", self.lwf), ("TDS", self.tds)):
            if not result.applicable:
                lines.append(f"{name}: not deducted - {result.reason}")
            elif not result.configured:
                lines.append(f"{name}: NOT CALCULATED - {result.reason}")
            elif result.reason:
                lines.append(f"{name}: ₹0 - {result.reason}")
        if self.pending_components:
            lines.append(
                "NOT YET CALCULATED: " + ", ".join(self.pending_components)
                + " (net pay and CTC are provisional)"
            )
        lines.extend(f"WARNING: {w}" for w in self.warnings)
        return "\n".join(lines)


class PayrollCalculationService:
    def __init__(self, session: Session) -> None:
        self.pf_service = PFService(session)
        self.esic_service = ESICService(session)
        self.pt_service = PTService(session)
        self.lwf_service = LWFService(session)
        self.tds_service = TDSService(session)
        self.salary_service = self.pf_service.salary_service

    def calculate(
        self,
        employee_code: str,
        period: PayrollPeriod,
        tds_override: Decimal | None = None,
        tds_override_reason: str | None = None,
        variable_earnings: Sequence[PayLine] = (),
        extra_deductions: Sequence[PayLine] = (),
    ) -> PayrollView:
        salary = self.salary_service.calculate(employee_code, period, variable_earnings)
        employee = self.salary_service.employees.get_by_code(employee_code)
        pf = self.pf_service.calculate_for_salary(employee, period, salary)
        esic = self.esic_service.calculate_for_salary(employee, period, salary)
        pt = self.pt_service.calculate_for_salary(employee, period, salary)
        lwf = self.lwf_service.calculate_for_salary(employee, period, salary)
        tds = self.tds_service.calculate_for_salary(
            employee, period, salary, pt, tds_override, tds_override_reason
        )
        heads = self.salary_service.salary.heads()
        arrears = salary.arrears
        summary = summarize_pay(
            salary.salary.earnings,
            [*pf.deductions(), *(arrears.deductions() if arrears else []), *esic.deductions(),
             *pt.deductions(), *lwf.deductions(), *tds.deductions(), *extra_deductions],
            [*pf.employer_contributions(),
             *(arrears.employer_contributions() if arrears else []),
             *esic.employer_contributions(), *lwf.employer_contributions()],
            heads,
        )  # fmt: skip
        warnings = [
            *salary.warnings, *pf.warnings, *esic.warnings, *pt.warnings, *lwf.warnings,
            *tds.warnings, *summary.warnings,
        ]  # fmt: skip
        for name, result in (("PT", pt), ("LWF", lwf), ("TDS", tds)):
            if result.applicable and not result.configured:
                where = getattr(result, "state_code", None)
                scope = f" for state {where}" if where else ""
                warnings.append(f"{name} NOT CALCULATED{scope}: {result.reason}")
        return PayrollView(
            employee_code=employee.employee_code,
            employee_name=employee.full_name,
            period=str(period),
            salary=salary,
            pf=pf,
            esic=esic,
            pt=pt,
            lwf=lwf,
            tds=tds,
            summary=summary,
            pending_components=list(PENDING_COMPONENTS),
            warnings=list(dict.fromkeys(warnings)),
        )
