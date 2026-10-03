"""Daily attendance rolled up into the monthly record the payroll engine works with.

LOP days for a month:
* HR's decision in ``lop_records`` when there is one (it always wins), otherwise
* approved leaves of type LOP.
An ABSENT day without an approved leave is an *exception*: it is never turned into
LOP automatically - HR decides (Phase 4 LOP flow) and the payslip warns until then.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Attendance, Leave, LopRecord
from payroll_copilot.domain.attendance import AttendanceRecord
from payroll_copilot.domain.enums import AttendanceStatus, LeaveType

HALF = Decimal("0.5")
ZERO = Decimal(0)


@dataclass(frozen=True)
class AttendanceException:
    day: date
    status: AttendanceStatus
    days: Decimal  # 1 for ABSENT, 0.5 for the absent half of a HALF_DAY
    remarks: str | None


class AttendanceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _days(self, employee_id: int, period: PayrollPeriod) -> list[Attendance]:
        return list(self.session.scalars(
            select(Attendance)
            .where(Attendance.employee_id == employee_id,
                   Attendance.day.between(period.start, period.end))
            .order_by(Attendance.day)
        ))  # fmt: skip

    def lop_record(self, employee_id: int, period: PayrollPeriod) -> LopRecord | None:
        return self.session.scalar(select(LopRecord).where(
            LopRecord.employee_id == employee_id, LopRecord.period == str(period)))  # fmt: skip

    def get(self, employee_id: int, period: PayrollPeriod) -> AttendanceRecord | None:
        days = self._days(employee_id, period)
        if not days:
            return None
        leave_ids = {d.leave_id for d in days if d.leave_id is not None}
        lop_leave_ids = set(self.session.scalars(
            select(Leave.id).where(Leave.id.in_(leave_ids), Leave.leave_type == LeaveType.LOP)
        )) if leave_ids else set()  # fmt: skip

        s = AttendanceStatus
        count = {status: ZERO for status in s}
        unpaid_leave = ZERO
        for d in days:
            count[d.status] += 1
            if d.status is s.LEAVE and d.leave_id in lop_leave_ids:
                unpaid_leave += 1
        week_offs, holidays = count[s.WEEK_OFF], count[s.HOLIDAY]
        record = self.lop_record(employee_id, period)
        return AttendanceRecord(
            period=str(period),
            calendar_days=period.calendar_days,
            working_days=Decimal(len(days)) - week_offs - holidays,
            week_offs=week_offs,
            holidays=holidays,
            present_days=count[s.PRESENT] + count[s.HALF_DAY] * HALF,
            paid_leave_days=count[s.LEAVE] - unpaid_leave,
            absent_days=count[s.ABSENT] + count[s.HALF_DAY] * HALF + unpaid_leave,
            lop_days=record.days if record is not None else unpaid_leave,
        )

    def lop_dates(self, employee_id: int, period: PayrollPeriod) -> tuple[dict, list[str]]:
        """Which days are LOP ({date: days}) - PF is split by date, so LOP needs dates.

        HR's recorded dates win; otherwise the decided LOP days are placed on the absences
        without approved leave in date order (half days count 0.5), and unpaid (LOP-type)
        approved leave days are LOP too.
        """
        warnings: list[str] = []
        days = self._days(employee_id, period)
        lop: dict = {}
        leave_ids = {d.leave_id for d in days if d.leave_id is not None}
        lop_leaves = set(self.session.scalars(
            select(Leave.id).where(Leave.id.in_(leave_ids), Leave.leave_type == LeaveType.LOP)
        )) if leave_ids else set()  # fmt: skip
        for d in days:
            if d.status is AttendanceStatus.LEAVE and d.leave_id in lop_leaves:
                lop[d.day] = Decimal(1)
        record = self.lop_record(employee_id, period)
        if record is None or record.days == 0:
            return lop, warnings
        if record.lop_dates:
            for item in record.lop_dates:
                day = date.fromisoformat(item["date"])
                lop[day] = lop.get(day, ZERO) + Decimal(str(item["days"]))
            return lop, warnings
        remaining = record.days
        for e in self.exceptions(employee_id, period):
            if remaining <= 0:
                break
            take = min(e.days, remaining)
            lop[e.day] = lop.get(e.day, ZERO) + take
            remaining -= take
        if remaining > 0:
            warnings.append(f"{remaining} LOP day(s) in {period} have no date (more LOP than "
                            "absences); record LOP dates so PF can be split correctly")  # fmt: skip
        return lop, warnings

    def exceptions(self, employee_id: int, period: PayrollPeriod) -> list[AttendanceException]:
        """Absences without an approved leave (candidates for LOP)."""
        return [
            AttendanceException(d.day, d.status, Decimal(1) if d.status is AttendanceStatus.ABSENT
                                else HALF, d.remarks)
            for d in self._days(employee_id, period)
            if d.status in (AttendanceStatus.ABSENT, AttendanceStatus.HALF_DAY)
        ]  # fmt: skip
