from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Attendance
from payroll_copilot.domain.attendance import AttendanceRecord


class AttendanceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, employee_id: int, period: PayrollPeriod) -> AttendanceRecord | None:
        row = self.session.scalar(
            select(Attendance).where(
                Attendance.employee_id == employee_id, Attendance.period == str(period)
            )
        )
        return AttendanceRecord.model_validate(row) if row else None
