"""Leave applications: an employee applies (chat or Leave page), the manager or a user who
can edit attendance approves, and approval writes the LEAVE days into attendance.

Days counted = the dates in the range minus weekly offs (Sunday) and days already marked
week off / holiday. Paid leave (CL / SL / EL) needs enough balance for the financial year;
LOP leave does not (it is unpaid). A finalized (locked) payroll month cannot get leave.
Approving leave for a month whose payslip is already processed recalculates that payslip.
Every step is audited.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.auth.permissions import Perm, has, roles_with
from payroll_copilot.core.dates import FinancialYear, PayrollPeriod
from payroll_copilot.core.exceptions import NotFoundError, PermissionDeniedError
from payroll_copilot.db.models import Attendance, Employee, Leave
from payroll_copilot.domain.enums import AttendanceStatus, LeaveStatus, LeaveType
from payroll_copilot.services import audit
from payroll_copilot.services.attendance_edit import WEEK_OFF_DAYS
from payroll_copilot.services.leave_service import leave_balances
from payroll_copilot.services.pay_adjustments import AdjustmentError, _not_locked

LABEL = {"CL": "Casual leave", "SL": "Sick leave", "EL": "Earned leave",
         "LOP": "Leave without pay (LOP)"}  # fmt: skip
BACKDATE_DAYS = 60  # leave can regularise absences up to this many days back
MAX_DAYS = 30


def _type(value: str) -> LeaveType:
    try:
        return LeaveType(str(value).upper())
    except ValueError:
        raise AdjustmentError(f"Unknown leave type {value!r} - use CL, SL, EL or LOP") from None


def _days_off(session: Session, employee: Employee, start: date, end: date) -> dict[date, str]:
    marked = {a.day: a.status for a in session.scalars(select(Attendance).where(
        Attendance.employee_id == employee.id, Attendance.day.between(start, end),
        Attendance.status.in_([AttendanceStatus.WEEK_OFF, AttendanceStatus.HOLIDAY])))}
    off: dict[date, str] = {}
    day = start
    while day <= end:
        if day in marked:
            off[day] = "holiday" if marked[day] is AttendanceStatus.HOLIDAY else "week off"
        elif day.weekday() in WEEK_OFF_DAYS:
            off[day] = "week off"
        day += timedelta(days=1)
    return off  # fmt: skip


def approvers(session: Session, employee: Employee) -> list[Employee]:
    """The manager plus users who can edit attendance - never the applicant."""
    staff = list(session.scalars(select(Employee).where(
        Employee.role.in_(roles_with(Perm.EDIT_ATTENDANCE)), Employee.login_enabled.is_(True))
        .order_by(Employee.employee_code)))  # fmt: skip
    manager = session.get(Employee, employee.manager_id) if employee.manager_id else None
    people = ([manager] if manager else []) + [e for e in staff if e is not manager]
    return [e for e in people if e.id != employee.id]


def can_decide(actor: Employee, employee: Employee) -> bool:
    if actor.id == employee.id:
        return False
    return has(actor, Perm.EDIT_ATTENDANCE) or employee.manager_id == actor.id


def plan(session: Session, employee: Employee, leave_type: str, start: date, end: date | None,
         today: date | None = None) -> dict[str, Any]:  # fmt: skip
    """Check an application and count its days (changes nothing)."""
    today = today or date.today()
    kind = _type(leave_type)
    end = end or start
    if end < start:
        raise AdjustmentError("The leave ends before it starts")
    if start < today - timedelta(days=BACKDATE_DAYS):
        raise AdjustmentError(f"Leave can be applied at most {BACKDATE_DAYS} days back")
    if start > today + timedelta(days=365):
        raise AdjustmentError("Leave can be applied at most a year ahead")
    if (end - start).days >= MAX_DAYS * 2:
        raise AdjustmentError(f"One application can cover at most {MAX_DAYS} working days")
    if start < employee.doj or (employee.lwd and end > employee.lwd):
        raise AdjustmentError(f"{employee.full_name} is not employed for the whole range")
    fy = FinancialYear.from_date(start)
    if end > fy.end:
        raise AdjustmentError(f"Split the leave at {fy.end:%d %b %Y} (financial year end)")
    for p in {PayrollPeriod.from_date(start), PayrollPeriod.from_date(end)}:
        _not_locked(session, employee, p)
    clash = session.scalar(select(Leave).where(
        Leave.employee_id == employee.id, Leave.from_date <= end, Leave.to_date >= start,
        Leave.status.in_([LeaveStatus.PENDING, LeaveStatus.APPROVED])))  # fmt: skip
    if clash is not None:
        raise AdjustmentError(f"You already have {clash.status.value.lower()} "
                              f"{LABEL[clash.leave_type.value]} from {clash.from_date:%d %b} "
                              f"to {clash.to_date:%d %b}")  # fmt: skip
    off = _days_off(session, employee, start, end)
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    working = [d for d in dates if d not in off]
    if not working:
        raise AdjustmentError("Those dates are all week offs or holidays - no leave needed")
    days = Decimal(len(working))
    if days > MAX_DAYS:
        raise AdjustmentError(f"One application can cover at most {MAX_DAYS} working days")
    out: dict[str, Any] = {
        "employee_code": employee.employee_code, "employee_name": employee.full_name,
        "leave_type": kind.value, "leave_label": LABEL[kind.value],
        "from": start.isoformat(), "to": end.isoformat(),
        "days": str(days), "dates": [d.isoformat() for d in working],
        "skipped": [{"date": d.isoformat(), "why": why} for d, why in sorted(off.items())],
        "financial_year": fy.label, "balance": None, "warnings": [],
        "approvers": [e.full_name for e in approvers(session, employee)][:4],
    }  # fmt: skip
    if kind is not LeaveType.LOP:
        bal = next((b for b in leave_balances(session, employee, fy) if b.leave_type == kind.value),
                   None)  # fmt: skip
        available = bal.available if bal else Decimal(0)
        out["balance"] = {"available": str(available), "after": str(available - days)}
        if days > available:
            raise AdjustmentError(f"Not enough {LABEL[kind.value]}: {available} day(s) available, "
                                  f"{days} asked. Apply the rest as LOP leave.")
    if start < today:
        out["warnings"].append("Back-dated: approval replaces the attendance of those days")
    return out


def as_dict(leave: Leave, employee: Employee | None = None) -> dict[str, Any]:
    return {"id": leave.id, "employee_code": employee.employee_code if employee else None,
            "employee_name": employee.full_name if employee else None,
            "leave_type": leave.leave_type.value, "leave_label": LABEL[leave.leave_type.value],
            "from": leave.from_date.isoformat(), "to": leave.to_date.isoformat(),
            "days": str(leave.days), "reason": leave.reason, "status": leave.status.value,
            "applied_at": leave.created_at.isoformat() if leave.created_at else None,
            "decided_at": leave.decided_at.isoformat() if leave.decided_at else None}  # fmt: skip


def apply(session: Session, employee: Employee, leave_type: str, start: date, end: date | None,
          reason: str, today: date | None = None,
          request_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    if len((reason or "").strip()) < 3:
        raise AdjustmentError("Give a short reason for the leave")
    checked = plan(session, employee, leave_type, start, end, today)
    leave = Leave(employee_id=employee.id, leave_type=LeaveType(checked["leave_type"]),
                  from_date=start, to_date=end or start, days=Decimal(checked["days"]),
                  reason=reason.strip()[:500], status=LeaveStatus.PENDING)  # fmt: skip
    session.add(leave)
    session.flush()
    audit.record(session, "leave.apply", actor=employee, entity="leave", entity_id=leave.id,
                 new={"employee_code": employee.employee_code, "leave_type": checked["leave_type"],
                      "from": checked["from"], "to": checked["to"], "days": checked["days"],
                      "reason": leave.reason}, request_id=request_id)  # fmt: skip
    return as_dict(leave, employee) | {"approvers": checked["approvers"]}


def _get(session: Session, leave_id: int) -> tuple[Leave, Employee]:
    leave = session.get(Leave, leave_id)
    if leave is None:
        raise NotFoundError(f"Leave request {leave_id} not found")
    return leave, session.get(Employee, leave.employee_id)


def cancel(session: Session, actor: Employee, leave_id: int,
           request_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    leave, employee = _get(session, leave_id)
    if employee.id != actor.id:
        raise PermissionDeniedError("You can only withdraw your own leave request")
    if leave.status is not LeaveStatus.PENDING:
        raise AdjustmentError(f"This request is already {leave.status.value.lower()}")
    leave.status = LeaveStatus.CANCELLED
    audit.record(session, "leave.cancel", actor=actor, entity="leave", entity_id=leave.id,
                 old={"status": "PENDING"}, new={"status": "CANCELLED"}, request_id=request_id)
    return as_dict(leave, employee)


def decide(session: Session, actor: Employee, leave_id: int, approve: bool, note: str,
           request_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    from payroll_copilot.services.payroll_actions import _recalculate

    leave, employee = _get(session, leave_id)
    if not can_decide(actor, employee):
        raise PermissionDeniedError("Only the employee's manager or HR can decide this leave"
                                    if actor.id != employee.id else
                                    "You cannot approve your own leave")  # fmt: skip
    if leave.status is not LeaveStatus.PENDING:
        raise AdjustmentError(f"This request is already {leave.status.value.lower()}")
    if not approve and len((note or "").strip()) < 3:
        raise AdjustmentError("Give a short note when rejecting")
    recalculated: list[str] = []
    if approve:
        months = sorted({str(PayrollPeriod.from_date(leave.from_date)),
                         str(PayrollPeriod.from_date(leave.to_date))})  # fmt: skip
        for m in months:
            _not_locked(session, employee, PayrollPeriod.parse(m))
        off = _days_off(session, employee, leave.from_date, leave.to_date)
        existing = {a.day: a for a in session.scalars(select(Attendance).where(
            Attendance.employee_id == employee.id,
            Attendance.day.between(leave.from_date, leave.to_date)))}  # fmt: skip
        changed = []
        day = leave.from_date
        while day <= leave.to_date:
            if day not in off:
                row = existing.get(day)
                changed.append({"date": day.isoformat(), "old": row.status.value if row else None})
                if row is None:
                    session.add(Attendance(employee_id=employee.id, day=day,
                                           status=AttendanceStatus.LEAVE,
                                           leave_id=leave.id, remarks="Approved leave"))
                else:
                    row.status, row.leave_id = AttendanceStatus.LEAVE, leave.id
                    row.remarks = "Approved leave"
            day += timedelta(days=1)
        session.flush()
        recalculated, _ = _recalculate(session, actor, employee, months, request_id)
        audit.record(session, "attendance.leave", actor=actor, entity="employee",
                     entity_id=employee.employee_code, new={"leave_id": leave.id, "days": changed},
                     request_id=request_id)  # fmt: skip
    leave.status = LeaveStatus.APPROVED if approve else LeaveStatus.REJECTED
    leave.decided_by_id = actor.id
    leave.decided_at = datetime.now(UTC)
    audit.record(session, "leave.approve" if approve else "leave.reject", actor=actor,
                 entity="leave", entity_id=leave.id, old={"status": "PENDING"},
                 new={"status": leave.status.value, "employee_code": employee.employee_code,
                      "note": (note or "").strip() or None, "recalculated": recalculated},
                 request_id=request_id)  # fmt: skip
    return as_dict(leave, employee) | {"recalculated": recalculated}


def mine(session: Session, employee: Employee, today: date | None = None) -> dict[str, Any]:
    fy = FinancialYear.from_date(today or date.today())
    rows = session.scalars(select(Leave).where(Leave.employee_id == employee.id)
                           .order_by(Leave.from_date.desc()).limit(30))  # fmt: skip
    return {"financial_year": fy.label,
            "balances": [b.as_dict() | {"label": LABEL[b.leave_type]}
                         for b in leave_balances(session, employee, fy)],
            "requests": [as_dict(r, employee) for r in rows],
            "approvers": [e.full_name for e in approvers(session, employee)][:4]}  # fmt: skip


def to_decide(session: Session, actor: Employee) -> list[dict[str, Any]]:
    rows = session.execute(select(Leave, Employee).join(Employee, Employee.id == Leave.employee_id)
                           .where(Leave.status == LeaveStatus.PENDING)
                           .order_by(Leave.from_date)).all()  # fmt: skip
    return [as_dict(leave, e) for leave, e in rows if can_decide(actor, e)]
