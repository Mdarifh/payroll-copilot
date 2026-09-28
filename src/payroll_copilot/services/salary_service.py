"""Earnings calculation for one employee and period (structure × paid days)."""

from collections.abc import Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from payroll_copilot.calculations.arrears import ArrearResult
from payroll_copilot.calculations.attendance import compute_paid_days, employed_span
from payroll_copilot.calculations.pay_summary import summarize_pay
from payroll_copilot.calculations.salary import (
    LopReversalInput,
    SalaryResult,
    SalarySegmentInput,
    calculate_earnings,
)
from payroll_copilot.calculations.trace import CalculationResult
from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import AttendanceError, NotFoundError
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import PaidDaysBasis
from payroll_copilot.domain.salary import PayLine, PaySummary, SalaryAssignmentInfo
from payroll_copilot.repositories.attendance_repo import AttendanceRepository
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.repositories.salary_repo import SalaryRepository
from payroll_copilot.services.arrear_service import ArrearService
from payroll_copilot.services.attendance_service import paid_days_basis

_FIXED_BASIS_DAYS = {PaidDaysBasis.FIXED_30: Decimal(30), PaidDaysBasis.FIXED_26: Decimal(26)}


class SalaryCalculationView(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    employee_name: str
    period: str
    attendance: CalculationResult
    salary: SalaryResult
    summary: PaySummary
    arrears: ArrearResult | None = None
    warnings: list[str] = Field(default_factory=list)

    def to_text(self) -> str:
        s, r = self.salary, self.salary.reduction
        lines = [
            f"Salary | {self.employee_code} ({self.employee_name}) | "
            f"{PayrollPeriod.parse(self.period).label}",
            f"Paid days {s.paid_days} of {s.basis_days} ({s.basis.value} basis)",
        ]
        for seg in s.segments:
            lines.append(
                f"  Segment {seg.label}: {seg.active_from} to {seg.active_to}, "
                f"gross {format_inr(seg.monthly_gross)}, payable {seg.payable_days} − "
                f"LOP {seg.lop_days} = {seg.paid_days} paid days"
            )
        lines.append("")
        lines.append(f"  {'Head':<22} {'Source':<30} {'Full month':>11} {'Days':>9} {'Amount':>10}")
        for line in s.lines:
            days = f"{line.paid_days}/{line.basis_days}" if line.is_prorated else "-"
            lines.append(
                f"  {line.name:<22} {line.source:<30} {format_inr(line.full_month_amount):>11} "
                f"{days:>9} {format_inr(line.amount):>10}"
            )
        lines.append(f"  {'GROSS EARNINGS':<74} {format_inr(s.gross_earnings):>10}")
        lines.append("")
        lines.append("Why gross differs from full-month salary:")
        lines.append(f"  Full-month gross            {format_inr(r['full_month_gross']):>12}")
        for key, label in (
            ("reduction_lop", "Less: LOP"),
            ("reduction_not_employed", "Less: days not employed"),
            ("reduction_mid_month_revision", "Less: mid-month revision"),
        ):
            if r[key]:
                lines.append(f"  {label:<28}{format_inr(-r[key]):>13}")
        if r["rounding_difference"]:
            lines.append(f"  {'Rounding':<28}{format_inr(r['rounding_difference']):>13}")
        lines.append(f"  {'Earned gross':<28}{format_inr(r['earned_gross']):>13}")
        for key, label in (
            ("lop_reversal", "Add: LOP reversal (arrear)"),
            ("arrears", "Add: salary arrears"),
            ("variable_earnings", "Add: variable earnings"),
        ):
            if r[key]:
                lines.append(f"  {label:<28}{format_inr(r[key]):>13}")
        lines.append("")
        lines.append(
            f"Net before statutory deductions: {format_inr(self.summary.net_pay)} "
            "(PF/ESIC/PT/LWF/TDS are added by their engines)"
        )
        lines.extend(f"WARNING: {w}" for w in self.warnings)
        return "\n".join(lines)


def _segment(
    a: SalaryAssignmentInfo, start, end, overrides: dict[str, tuple[Decimal, str]]
) -> SalarySegmentInput:
    components = [
        PayLine(head_code=c.head_code, amount=overrides[c.head_code][0])
        if c.head_code in overrides else c
        for c in a.components
    ]  # fmt: skip
    return SalarySegmentInput(
        label=f"{a.structure.code} from {a.effective_from}",
        active_from=max(start, a.effective_from),
        active_to=min(end, a.effective_to) if a.effective_to else end,
        monthly_gross=sum((c.amount for c in components), Decimal(0)),
        components=components,
    )


class SalaryService:
    def __init__(self, session: Session) -> None:
        self.employees = EmployeeRepository(session)
        self.salary = SalaryRepository(session)
        self.attendance = AttendanceRepository(session)
        self.arrears = ArrearService(session)

    def as_paid_assignments(
        self, employee: Employee, month: PayrollPeriod, start, end
    ) -> tuple[list[SalarySegmentInput], list[str]]:
        """Segments for ``month`` as they were paid (revisions paid later as arrear excluded)."""
        overrides = self.arrears.as_paid_overrides(employee, month)
        segments = [
            _segment(a, start, end, overrides)
            for a in self.salary.assignments_between(employee.id, start, end)
        ]
        notes = [
            f"As paid in {month}: {head} at old amount {old}; revision paid as arrear in {paid_in}"
            for head, (old, paid_in) in overrides.items()
        ]
        return segments, notes

    def _lop_reversal(
        self, employee: Employee, period: PayrollPeriod, days: Decimal, basis: PaidDaysBasis
    ) -> tuple[LopReversalInput, str]:
        prev = period.previous()
        span = employed_span(prev, employee.doj, employee.lwd)
        if span is None:
            raise AttendanceError(
                f"LOP reversal in {period} but {employee.employee_code} was not employed in {prev}"
            )
        prev_record = self.attendance.get(employee.id, prev)
        if prev_record is not None:
            basis_days = compute_paid_days(
                prev_record, doj=employee.doj, lwd=employee.lwd, basis=basis
            ).breakdown["basis_days"]
        elif basis is PaidDaysBasis.CALENDAR:
            basis_days = Decimal(prev.calendar_days)
        elif basis in _FIXED_BASIS_DAYS:
            basis_days = _FIXED_BASIS_DAYS[basis]
        else:
            raise NotFoundError(f"LOP reversal needs attendance for {prev} (WORKING_DAYS basis)")
        assignments = self.salary.assignments_between(employee.id, span[1], span[1])
        if not assignments:
            raise NotFoundError(f"No salary structure on {span[1]} for LOP reversal")
        reversal = LopReversalInput(
            period=str(prev), days=days, basis_days=basis_days,
            components=assignments[-1].components,
        )  # fmt: skip
        note = (
            f"LOP reversal of {days} days assumed to relate to {prev}; paid at {prev} "
            f"rate (structure on {span[1]}, basis {basis_days} days)"
        )
        return reversal, note

    def calculate(
        self,
        employee_code: str,
        period: PayrollPeriod,
        variable_earnings: Sequence[PayLine] = (),
    ) -> SalaryCalculationView:
        settings = get_settings()
        employee = self.employees.get_by_code(employee_code)
        heads = self.salary.heads()
        basis = paid_days_basis(employee)
        record = self.attendance.get(employee.id, period)
        if record is None:
            raise NotFoundError(f"No attendance for {employee.employee_code} in {period}")
        paid = compute_paid_days(
            record, doj=employee.doj, lwd=employee.lwd, basis=basis,
            employee_code=employee.employee_code,
        )  # fmt: skip

        warnings = list(paid.warnings)
        span = employed_span(period, employee.doj, employee.lwd)
        segments: list[SalarySegmentInput] = []
        if span:
            segments, notes = self.as_paid_assignments(employee, period, *span)
            warnings.extend(notes)
        arrears = self.arrears.for_payment(employee, period, heads, basis)
        if arrears:
            warnings.extend(arrears.warnings)

        reversal = None
        if record.lop_reversal_days > 0:
            reversal, note = self._lop_reversal(employee, period, record.lop_reversal_days, basis)
            warnings.append(note)

        result = calculate_earnings(
            segments, paid, heads,
            basis=basis,
            rounding_mode=settings.default_rounding_mode,
            lop_allocation=settings.default_lop_allocation,
            lop_reversal=reversal,
            variable_earnings=variable_earnings,
            arrear_lines=arrears.lines if arrears else (),
            employee_code=employee.employee_code,
        )  # fmt: skip
        warnings.extend(result.warnings)
        return SalaryCalculationView(
            employee_code=employee.employee_code,
            employee_name=employee.full_name,
            period=str(period),
            attendance=paid,
            salary=result,
            summary=summarize_pay(result.earnings, [], [], heads),
            arrears=arrears,
            warnings=list(dict.fromkeys(warnings)),
        )
