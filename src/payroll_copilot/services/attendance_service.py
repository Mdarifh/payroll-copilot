"""Attendance and paid days for one employee and period."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from payroll_copilot.calculations.attendance import (
    PaidDaysDiagnosis,
    compute_paid_days,
    diagnose_paid_days,
)
from payroll_copilot.calculations.trace import CalculationResult
from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.attendance import AttendanceRecord
from payroll_copilot.domain.enums import PaidDaysBasis
from payroll_copilot.repositories.attendance_repo import AttendanceRepository
from payroll_copilot.repositories.employee_repo import EmployeeRepository


class AttendanceView(BaseModel):
    model_config = ConfigDict(frozen=True)

    employee_code: str
    employee_name: str
    basis: PaidDaysBasis
    record: AttendanceRecord
    paid_days: CalculationResult

    def to_text(self) -> str:
        r = self.record
        b = self.paid_days.breakdown
        lines = [
            f"Attendance | {self.employee_code} ({self.employee_name}) | "
            f"{PayrollPeriod.parse(r.period).label} | basis {self.basis.value}",
            f"  Calendar days {r.calendar_days} | Working {r.working_days} | "
            f"Week offs {r.week_offs} | Holidays {r.holidays}",
            f"  Present {r.present_days} | Paid leave {r.paid_leave_days} | "
            f"Absent {r.absent_days} | LOP {r.lop_days} | LOP reversal {r.lop_reversal_days}",
            f"  Paid days {b['paid_days']} of {b['basis_days']} "
            f"(proration factor {b['paid_days']}/{b['basis_days']})",
            "",
            self.paid_days.to_text(),
        ]
        return "\n".join(lines)


def paid_days_basis(employee: Employee) -> PaidDaysBasis:
    """Employee override, else company default (DEFAULT_PAID_DAYS_BASIS)."""
    return employee.paid_days_basis or get_settings().default_paid_days_basis


class AttendanceService:
    def __init__(self, session: Session) -> None:
        self.employees = EmployeeRepository(session)
        self.attendance = AttendanceRepository(session)

    def _load(self, employee_code: str, period: PayrollPeriod) -> tuple[Employee, AttendanceRecord]:
        employee = self.employees.get_by_code(employee_code)
        record = self.attendance.get(employee.id, period)
        if record is None:
            raise NotFoundError(f"No attendance for {employee.employee_code} in {period}")
        return employee, record

    def get_attendance(self, employee_code: str, period: PayrollPeriod) -> AttendanceView:
        employee, record = self._load(employee_code, period)
        basis = paid_days_basis(employee)
        result = compute_paid_days(
            record, doj=employee.doj, lwd=employee.lwd, basis=basis,
            employee_code=employee.employee_code,
        )  # fmt: skip
        return AttendanceView(
            employee_code=employee.employee_code,
            employee_name=employee.full_name,
            basis=basis,
            record=record,
            paid_days=result,
        )

    def diagnose_paid_days(
        self, employee_code: str, period: PayrollPeriod, actual_paid_days: Decimal
    ) -> PaidDaysDiagnosis:
        employee, record = self._load(employee_code, period)
        return diagnose_paid_days(
            record, actual_paid_days, doj=employee.doj, lwd=employee.lwd,
            basis=paid_days_basis(employee), employee_code=employee.employee_code,
        )  # fmt: skip
