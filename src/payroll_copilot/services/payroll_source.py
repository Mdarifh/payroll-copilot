"""Where bulk payroll reads employees and attendance from.

``LocalPayrollSource`` reads this app's own database (never an external/Zimyo system).
Every call goes through the rate limiter and the retry helper, so a real payroll API
with rate limits could be plugged in later behind the same interface.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Attendance, Employee, LopRecord, PayrollRun
from payroll_copilot.domain.enums import AttendanceStatus
from payroll_copilot.services.reliability import RateLimiter, with_retry

STATE_NAMES = {
    "andhra pradesh": "AP", "assam": "AS", "bihar": "BR", "chandigarh": "CH", "delhi": "DL",
    "new delhi": "DL", "gujarat": "GJ", "himachal pradesh": "HP", "haryana": "HR",
    "jharkhand": "JH", "karnataka": "KA", "bangalore": "KA", "bengaluru": "KA", "kerala": "KL",
    "maharashtra": "MH", "mumbai": "MH", "pune": "MH", "meghalaya": "ML", "manipur": "MN",
    "madhya pradesh": "MP", "mizoram": "MZ", "nagaland": "NL", "odisha": "OD", "punjab": "PB",
    "puducherry": "PY", "rajasthan": "RJ", "sikkim": "SK", "telangana": "TG",
    "hyderabad": "TG", "tamil nadu": "TN", "chennai": "TN", "tripura": "TR",
    "uttarakhand": "UK", "uttar pradesh": "UP", "noida": "UP", "west bengal": "WB",
    "kolkata": "WB", "gurgaon": "HR", "gurugram": "HR",
}  # fmt: skip


def normalise_state(value: str | None) -> str | None:
    """'Delhi' / 'delhi office' / 'DL' -> 'DL'. Unknown names raise ValueError."""
    if not value:
        return None
    text = value.strip().lower().removesuffix(" office").strip()
    if text in STATE_NAMES:
        return STATE_NAMES[text]
    if len(text) == 2 and text.upper() in set(STATE_NAMES.values()):
        return text.upper()
    raise ValueError(f"Unknown state or office {value!r} - use a state name or code like DL")


@dataclass(frozen=True)
class EmployeeRow:
    id: int
    employee_code: str
    full_name: str
    department: str | None
    work_state: str


class PayrollSource(Protocol):
    def employees_page(self, period: PayrollPeriod, department: str | None,
                       work_state: str | None, page: int, size: int) -> list[EmployeeRow]: ...

    def absences(self, employee_ids: list[int],
                 period: PayrollPeriod) -> dict[int, list[tuple[date, Decimal]]]: ...

    def lop_decided(self, employee_ids: list[int], period: PayrollPeriod) -> set[int]: ...

    def processed(self, employee_ids: list[int], period: PayrollPeriod) -> set[int]: ...


class LocalPayrollSource:
    def __init__(self, session: Session, limiter: RateLimiter | None = None,
                 attempts: int = 3) -> None:  # fmt: skip
        self.session = session
        self.limiter = limiter
        self.attempts = attempts

    def _call(self, fn):
        def guarded():
            if self.limiter is not None:
                self.limiter.acquire()
            return fn()

        return with_retry(guarded, attempts=self.attempts)

    def employees_page(
        self, period: PayrollPeriod, department: str | None, work_state: str | None,
        page: int, size: int,
    ) -> list[EmployeeRow]:  # fmt: skip
        stmt = (
            select(Employee.id, Employee.employee_code, Employee.full_name,
                   Employee.department, Employee.work_state)
            .where(Employee.doj <= period.end,
                   or_(Employee.lwd.is_(None), Employee.lwd >= period.start))
            .order_by(Employee.id).offset(page * size).limit(size)
        )  # fmt: skip
        if department:
            stmt = stmt.where(func.lower(Employee.department) == department.strip().lower())
        if work_state:
            stmt = stmt.where(Employee.work_state == work_state)
        rows = self._call(lambda: self.session.execute(stmt).all())
        return [EmployeeRow(*r) for r in rows]

    def absences(self, employee_ids: list[int],
                 period: PayrollPeriod) -> dict[int, list[tuple[date, Decimal]]]:  # fmt: skip
        if not employee_ids:
            return {}
        stmt = (
            select(Attendance.employee_id, Attendance.day, Attendance.status)
            .where(Attendance.employee_id.in_(employee_ids),
                   Attendance.day.between(period.start, period.end),
                   Attendance.status.in_([AttendanceStatus.ABSENT, AttendanceStatus.HALF_DAY]))
            .order_by(Attendance.employee_id, Attendance.day)
        )  # fmt: skip
        result: dict[int, list[tuple[date, Decimal]]] = defaultdict(list)
        for employee_id, day, status in self._call(lambda: self.session.execute(stmt).all()):
            days = Decimal(1) if status is AttendanceStatus.ABSENT else Decimal("0.5")
            result[employee_id].append((day, days))
        return dict(result)

    def lop_decided(self, employee_ids: list[int], period: PayrollPeriod) -> set[int]:
        stmt = select(LopRecord.employee_id).where(
            LopRecord.employee_id.in_(employee_ids), LopRecord.period == str(period))
        return set(self._call(lambda: self.session.scalars(stmt).all()))  # fmt: skip

    def processed(self, employee_ids: list[int], period: PayrollPeriod) -> set[int]:
        stmt = select(PayrollRun.employee_id).where(
            PayrollRun.employee_id.in_(employee_ids), PayrollRun.period == str(period))
        return set(self._call(lambda: self.session.scalars(stmt).all()))  # fmt: skip
