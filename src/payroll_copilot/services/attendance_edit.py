"""Manual attendance correction by HR: change a day's status, or create a month's
attendance when none was recorded.

Goes through the same preview -> confirm (-> approval) flow as every payroll change
(simulation.PayrollChange.attendance / fill_month). Leave days are not set here: they
come from approved leave applications, so a manual "LEAVE" without a leave record is
refused. Every change is audited with the old and new status of each day.
"""

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Attendance, Employee
from payroll_copilot.domain.enums import AttendanceStatus
from payroll_copilot.services import audit
from payroll_copilot.services.pay_adjustments import AdjustmentError, _not_locked

EDITABLE = (AttendanceStatus.PRESENT, AttendanceStatus.ABSENT, AttendanceStatus.HALF_DAY,
            AttendanceStatus.WEEK_OFF, AttendanceStatus.HOLIDAY)  # fmt: skip
WEEK_OFF_DAYS = (6,)  # Sunday (Monday = 0): weekly off when a month is created


def _status(value: str) -> AttendanceStatus:
    try:
        status = AttendanceStatus(value.upper())
    except ValueError:
        raise AdjustmentError(f"Unknown attendance status {value!r}") from None
    if status not in EDITABLE:
        raise AdjustmentError("Leave days come from approved leave applications; mark the day "
                              "Present, Absent, Half day, Week off or Holiday")  # fmt: skip
    return status


def update_days(session: Session, actor: Employee, employee: Employee, period: PayrollPeriod,
                days: list[dict[str, Any]], fill_month: bool, reason: str,
                today: date | None = None,
                request_id: str | None = None) -> list[dict[str, str]]:  # fmt: skip
    """Set the given days (and, with ``fill_month``, create the missing days of the month:
    Sundays week off, other employed days present). Returns the changes made."""
    today = today or date.today()
    if len((reason or "").strip()) < 5:
        raise AdjustmentError("A reason (at least 5 characters) is required")
    _not_locked(session, employee, period)
    existing = {a.day: a for a in session.scalars(select(Attendance).where(
        Attendance.employee_id == employee.id,
        Attendance.day.between(period.start, period.end)))}  # fmt: skip
    first = max(period.start, employee.doj)
    last = min(period.end, employee.lwd or period.end, today)
    wanted: dict[date, tuple[AttendanceStatus, str | None]] = {}
    if fill_month:
        day = first
        while day <= last:
            if day not in existing:
                wanted[day] = (AttendanceStatus.WEEK_OFF if day.weekday() in WEEK_OFF_DAYS
                               else AttendanceStatus.PRESENT, None)  # fmt: skip
            day += timedelta(days=1)
    for item in days:
        day = item["date"] if isinstance(item["date"], date) else date.fromisoformat(item["date"])
        if not period.start <= day <= period.end:
            raise AdjustmentError(f"{day} is not in {period.label}")
        if day < employee.doj or (employee.lwd and day > employee.lwd):
            raise AdjustmentError(f"{employee.full_name} was not employed on {day}")
        if day > today:
            raise AdjustmentError(f"Attendance cannot be marked for a future date ({day})")
        wanted[day] = (_status(str(item["status"])), (item.get("remarks") or None))
    if not wanted:
        raise AdjustmentError("Nothing to change: all days already have attendance")
    changes = []
    for day, (status, remarks) in sorted(wanted.items()):
        row = existing.get(day)
        old = None if row is None else row.status.value
        if row is None:
            session.add(Attendance(employee_id=employee.id, day=day, status=status,
                                   remarks=remarks))  # fmt: skip
        else:
            if row.status is AttendanceStatus.LEAVE:
                raise AdjustmentError(f"{day} is an approved leave day; cancel the leave instead")
            if row.status is status and (remarks is None or remarks == row.remarks):
                continue
            row.status = status
            if remarks is not None:
                row.remarks = remarks
        changes.append({"date": day.isoformat(), "old": old or "-", "new": status.value})
    if not changes:
        raise AdjustmentError("Nothing to change: the days already have that status")
    session.flush()
    audit.record(session, "attendance.update", actor=actor, entity="employee",
                 entity_id=employee.employee_code,
                 new={"period": str(period), "changes": changes, "reason": reason.strip(),
                      "fill_month": fill_month}, request_id=request_id)  # fmt: skip
    return changes


def create_for_month(session: Session, actor: Employee, period: PayrollPeriod, reason: str,
                     department: str | None = None, work_state: str | None = None,
                     today: date | None = None) -> dict[str, Any]:  # fmt: skip
    """Create the missing attendance days of the month (up to today) for every employee in
    scope: Sundays week off, other employed days present. Existing days are never changed;
    locked payslips are skipped. One audit row with the counts."""
    from sqlalchemy import or_

    from payroll_copilot.db.models import PayrollRun
    from payroll_copilot.domain.enums import PayrollRunStatus

    today = today or date.today()
    if len((reason or "").strip()) < 5:
        raise AdjustmentError("A reason (at least 5 characters) is required")
    if period.start > today:
        raise AdjustmentError(f"{period.label} has not started yet")
    stmt = select(Employee).where(Employee.doj <= period.end,
                                  or_(Employee.lwd.is_(None), Employee.lwd >= period.start))
    if department:
        stmt = stmt.where(Employee.department == department)
    if work_state:
        stmt = stmt.where(Employee.work_state == work_state)
    locked = set(session.scalars(select(PayrollRun.employee_id).where(
        PayrollRun.period == str(period), PayrollRun.status == PayrollRunStatus.LOCKED)))
    employees, days_added = 0, 0
    for e in session.scalars(stmt):
        if e.id in locked:
            continue
        have = set(session.scalars(select(Attendance.day).where(
            Attendance.employee_id == e.id, Attendance.day.between(period.start, period.end))))
        day, last = max(period.start, e.doj), min(period.end, e.lwd or period.end, today)
        added = 0
        while day <= last:
            if day not in have:
                session.add(Attendance(employee_id=e.id, day=day, status=(
                    AttendanceStatus.WEEK_OFF if day.weekday() in WEEK_OFF_DAYS
                    else AttendanceStatus.PRESENT)))  # fmt: skip
                added += 1
            day += timedelta(days=1)
        if added:
            employees += 1
            days_added += added
    if not days_added:
        raise AdjustmentError("Every employee already has attendance up to "
                              f"{min(today, period.end)}")
    session.flush()
    audit.record(session, "attendance.bulk_create", actor=actor, entity="payroll_month",
                 entity_id=str(period), new={"employees": employees, "days": days_added,
                                             "up_to": min(today, period.end),
                                             "reason": reason.strip()})  # fmt: skip
    return {"period": str(period), "employees": employees, "days": days_added,
            "up_to": min(today, period.end).isoformat()}
