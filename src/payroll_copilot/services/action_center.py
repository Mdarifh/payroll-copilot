"""Action Center: submit, approve, reject and cancel payroll changes (maker-checker).

submit  - the requester confirmed a preview (preview id checked). With company setting
          change_approval on, the change waits for a DIFFERENT HR user; otherwise it is
          applied at once. Either way an ActionRequest records it.
approve - the approver confirms a fresh preview (data may have moved since the request);
          the change is applied through payroll_actions.apply (recalculation + audit).
          A change that no longer validates becomes FAILED with the reason.
reject / cancel - with a note; nothing is applied.
"""

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from payroll_copilot.auth.permissions import Perm, change_perm, has, roles_with
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import (
    NotFoundError,
    PayrollCopilotError,
    PermissionDeniedError,
)
from payroll_copilot.db.base import iso_utc
from payroll_copilot.db.models import Employee
from payroll_copilot.db.models.actions import (
    APPLIED,
    CANCELLED,
    FAILED,
    PENDING,
    REJECTED,
    STATUSES,
    ActionRequest,
)
from payroll_copilot.services import audit
from payroll_copilot.services.company_config import get_config
from payroll_copilot.services.payroll_actions import StalePreviewError, apply
from payroll_copilot.services.simulation import PayrollChange, simulate

KIND_TITLE = {"lop": "LOP", "overtime": "Overtime", "adjustment": "One-off pay",
              "salary": "Salary revision", "attendance": "Attendance"}  # fmt: skip


class ActionConflictError(PayrollCopilotError):
    """The action is not in a state that allows this (e.g. already decided)."""


def _now() -> datetime:
    return datetime.now(UTC)


def kind_of(change: PayrollChange) -> str:
    if change.salary is not None:
        return "salary"
    if change.cancel_entries and not (change.overtime or change.adjustments or change.attendance
                                      or change.lop_days is not None):
        return ("overtime" if all(c.kind == "overtime" for c in change.cancel_entries)
                else "adjustment")
    if change.attendance or change.fill_month:
        return "attendance"
    if change.overtime:
        return "overtime"
    if change.adjustments:
        return "adjustment"
    return "lop"


def _title(change: PayrollChange, preview: dict[str, Any]) -> str:
    rows = {r["key"]: r for r in preview["rows"]}
    kind = kind_of(change)
    if change.cancel_entries and kind in ("overtime", "adjustment") and not (
            change.overtime or change.adjustments):
        return f"Undo: cancel {len(change.cancel_entries)} {kind} entr" + (
            "y" if len(change.cancel_entries) == 1 else "ies")
    if kind == "attendance":
        days = ", ".join(f"{a.date:%d %b} → {a.status.replace('_', ' ').title()}"
                         for a in change.attendance[:3])
        more = f" (+{len(change.attendance) - 3} more)" if len(change.attendance) > 3 else ""
        fill = "Create missing attendance days" if change.fill_month else ""
        extra = ("" if change.lop_days is None
                 else f"; LOP → {change.lop_days.normalize():f} days")
        return "Attendance: " + "; ".join(x for x in (fill, days + more) if x) + extra
    if kind == "lop":
        r = rows.get("lop_days")
        return f"LOP {r['before']} → {r['after']} days" if r else "LOP change"
    if kind == "overtime":
        return "Overtime +" + " + ".join(f"{o.hours.normalize():f} h" for o in change.overtime)
    if kind == "adjustment":
        a = change.adjustments[0]
        sign = "−" if a.kind.upper() == "DEDUCTION" else "+"
        return f"{a.label or a.head_code} {sign}₹{a.amount.normalize():f}"
    return (f"Salary → ₹{change.salary.monthly_gross.normalize():f} gross from "
            f"{change.salary.effective_from:%d %b %Y}")


def _get(session: Session, public_id: str) -> ActionRequest:
    row = session.scalar(select(ActionRequest).where(ActionRequest.public_id == public_id))
    if row is None:
        raise NotFoundError(f"Action {public_id} not found")
    return row


def submit(session: Session, actor: Employee, employee_code: str, period: PayrollPeriod,
           change: PayrollChange, preview_id: str, source: str = "ui",
           request_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The requester confirmed ``preview_id``: record it, and apply now or queue it."""
    if len(change.reason.strip()) < 5 or change.reason.strip() == "What-if preview":
        raise PayrollCopilotError("A reason (at least 5 characters) is required")
    current = simulate(session, actor, employee_code, period, change)
    if current["preview_id"] != preview_id:
        raise StalePreviewError("Payroll data changed after the preview. Please preview the "
                                "change again and confirm the new numbers.")  # fmt: skip
    employee = session.scalar(select(Employee).where(
        Employee.employee_code == employee_code.upper()))  # fmt: skip
    needed = change_perm(kind_of(change))
    if not has(actor, needed):
        raise PermissionDeniedError(f"Your role cannot request this change ({needed.value})")
    row = ActionRequest(
        public_id=uuid4().hex[:16], employee_id=employee.id, period=str(period),
        kind=kind_of(change), title=_title(change, current)[:200],
        change=change.model_dump(mode="json"),
        preview={"rows": current["rows"], "net_change": current["net_change"],
                 "warnings": current["warnings"], "overtime": current["overtime"]},
        preview_id=preview_id, reason=change.reason.strip(), source=source,
        requested_by_id=actor.id,
    )  # fmt: skip
    session.add(row)
    session.flush()
    audit.record(session, "action.submit", actor=actor, entity="action_request",
                 entity_id=row.public_id, new={"employee_code": employee.employee_code,
                                               "period": row.period, "title": row.title,
                                               "net_change": current["net_change"],
                                               "reason": row.reason, "source": source},
                 request_id=request_id)  # fmt: skip
    if get_config(session, date.today()).get("change_approval", True):  # workflow: today's value
        return {**detail(session, row), "message": "Sent for approval by another HR user"}
    return _apply(session, actor, row, preview_id, request_id)


def _apply(session: Session, actor: Employee, row: ActionRequest, preview_id: str,
           request_id: str | None) -> dict[str, Any]:  # fmt: skip
    employee = session.get(Employee, row.employee_id)
    change = PayrollChange.model_validate(row.change)
    before = _snapshot(session, employee, PayrollPeriod.parse(row.period), change)
    try:
        with session.begin_nested():  # a failed apply leaves nothing half-done
            result = apply(session, actor, employee.employee_code,
                           PayrollPeriod.parse(row.period), change, preview_id, request_id)
    except StalePreviewError:
        raise
    except PayrollCopilotError as exc:
        row.status, row.error = FAILED, str(exc)[:500]
        row.decided_by_id, row.decided_at = actor.id, _now()
        audit.record(session, "action.failed", actor=actor, entity="action_request",
                     entity_id=row.public_id, new={"error": row.error})  # fmt: skip
        session.flush()
        return {**detail(session, row), "message": f"Could not apply: {exc}"}
    row.status, row.applied_at = APPLIED, _now()
    if row.requested_by_id != actor.id:
        row.decided_by_id, row.decided_at = actor.id, row.applied_at
    undo, note = _reverse(session, employee, PayrollPeriod.parse(row.period), change, before)
    row.result = {"net_change": result["net_change"], "recalculated": result["recalculated"],
                  "not_recalculated": result["not_recalculated"], "rows": result["rows"],
                  "undo": undo, "undo_note": note}
    session.flush()
    return {**detail(session, row), "message": "Applied"}


def fresh_preview(session: Session, actor: Employee, public_id: str) -> dict[str, Any]:
    """Today's numbers for a pending action (what the approver confirms)."""
    row = _get(session, public_id)
    if row.status != PENDING:
        raise ActionConflictError(f"Action is {row.status.lower().replace('_', ' ')}")
    employee = session.get(Employee, row.employee_id)
    return simulate(session, actor, employee.employee_code, PayrollPeriod.parse(row.period),
                    PayrollChange.model_validate(row.change))  # fmt: skip


def approve(session: Session, actor: Employee, public_id: str, preview_id: str,
            request_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    row = _get(session, public_id)
    if row.status != PENDING:
        raise ActionConflictError(f"Action is already {row.status.lower().replace('_', ' ')}")
    if row.requested_by_id == actor.id:
        raise PermissionDeniedError("A change must be approved by a different HR user than the "
                                    "one who requested it (maker-checker)")  # fmt: skip
    audit.record(session, "action.approve", actor=actor, entity="action_request",
                 entity_id=row.public_id, request_id=request_id)  # fmt: skip
    return _apply(session, actor, row, preview_id, request_id)


def reject(session: Session, actor: Employee, public_id: str, note: str) -> dict[str, Any]:
    row = _get(session, public_id)
    if row.status != PENDING:
        raise ActionConflictError(f"Action is already {row.status.lower().replace('_', ' ')}")
    if len((note or "").strip()) < 5:
        raise PayrollCopilotError("A note (at least 5 characters) is required")
    cancelling = row.requested_by_id == actor.id  # the requester withdraws their own request
    if not cancelling and not has(actor, Perm.APPROVE):
        raise PermissionDeniedError("Only a role that can approve changes may reject them")
    row.status = CANCELLED if cancelling else REJECTED
    row.decided_by_id, row.decided_at, row.decision_note = actor.id, _now(), note.strip()
    audit.record(session, "action.cancel" if cancelling else "action.reject", actor=actor,
                 entity="action_request", entity_id=row.public_id,
                 new={"note": row.decision_note})  # fmt: skip
    session.flush()
    return detail(session, row)


def detail(session: Session, row: ActionRequest) -> dict[str, Any]:
    people = {e.id: e for e in session.scalars(select(Employee).where(
        Employee.id.in_({row.employee_id, row.requested_by_id, row.decided_by_id or 0})))}
    names = {i: e.full_name for i, e in people.items()}
    employee = session.get(Employee, row.employee_id)
    return {
        "id": row.public_id, "status": row.status, "kind": row.kind,
        "kind_label": KIND_TITLE.get(row.kind, row.kind), "title": row.title,
        "employee_code": employee.employee_code, "employee_name": names.get(row.employee_id),
        "period": row.period, "period_label": PayrollPeriod.parse(row.period).label,
        "reason": row.reason, "source": row.source, "change": row.change,
        "preview": row.preview, "net_change": row.preview.get("net_change"),
        "requested_by": names.get(row.requested_by_id),
        "requested_by_code": people[row.requested_by_id].employee_code,
        "requested_at": iso_utc(row.created_at),
        "decided_by": names.get(row.decided_by_id), "decided_at": iso_utc(row.decided_at),
        "decision_note": row.decision_note, "applied_at": iso_utc(row.applied_at),
        "result": row.result, "error": row.error,
    }  # fmt: skip


def list_actions(session: Session, status: str | None = None, page: int = 1,
                 page_size: int = 20, search: str | None = None) -> dict[str, Any]:  # fmt: skip
    emp = aliased(Employee)
    stmt = select(ActionRequest).join(emp, emp.id == ActionRequest.employee_id)
    if status:
        if status.upper() not in STATUSES:
            raise PayrollCopilotError(f"status must be one of {', '.join(STATUSES)}")
        stmt = stmt.where(ActionRequest.status == status.upper())
    if search and search.strip():
        like = f"%{search.strip().lower()}%"
        stmt = stmt.where(func.lower(emp.full_name).like(like)
                          | func.lower(emp.employee_code).like(like))
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = session.scalars(stmt.order_by(ActionRequest.id.desc())
                           .offset((page - 1) * page_size).limit(page_size))  # fmt: skip
    counts = dict(session.execute(select(ActionRequest.status, func.count())
                                  .group_by(ActionRequest.status)).all())  # fmt: skip
    return {"total": total, "page": page, "pages": max(1, -(-total // page_size)),
            "counts": {s: counts.get(s, 0) for s in STATUSES},
            "items": [detail(session, r) for r in rows]}  # fmt: skip


def pending_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(ActionRequest).where(
        ActionRequest.status == PENDING)) or 0



def approval_setting(session: Session, actor: Employee | None = None) -> dict[str, Any]:
    """The setting, and the HR users who can approve the actor's requests (anyone but them)."""
    approvers = session.scalars(select(Employee).where(
        Employee.role.in_(roles_with(Perm.APPROVE)), Employee.login_enabled.is_(True),
        Employee.id != (actor.id if actor else 0)).order_by(Employee.full_name))
    return {"change_approval": bool(get_config(session, date.today()).get("change_approval",
                                                                           True)),
            "approvers": [{"name": e.full_name, "email": e.email} for e in approvers]}


def set_approval(session: Session, actor: Employee, enabled: bool, reason: str) -> dict[str, Any]:
    """Turn maker-checker for payroll changes on or off (from today, audited)."""
    from payroll_copilot.services.company_config import set_setting

    if len((reason or "").strip()) < 5:
        raise PayrollCopilotError("A reason (at least 5 characters) is required")
    set_setting(session, "change_approval", enabled, date.today(), actor)
    audit.record(session, "settings.change_approval", actor=actor, entity="company_setting",
                 entity_id="change_approval", new={"enabled": enabled, "reason": reason.strip()})
    return approval_setting(session, actor)


# ---------------------------------------------------------------------- undo


def _entry_ids(session: Session, employee: Employee, period: PayrollPeriod) -> dict[str, set]:
    from payroll_copilot.db.models import OvertimeEntry, PayAdjustment

    return {"overtime": set(session.scalars(select(OvertimeEntry.id).where(
                OvertimeEntry.employee_id == employee.id, OvertimeEntry.period == str(period)))),
            "adjustment": set(session.scalars(select(PayAdjustment.id).where(
                PayAdjustment.employee_id == employee.id, PayAdjustment.period == str(period))))}


def _snapshot(session: Session, employee: Employee, period: PayrollPeriod,
              change: PayrollChange) -> dict[str, Any]:  # fmt: skip
    """What the change is about to overwrite (to build its exact reverse)."""
    from payroll_copilot.db.models import Attendance
    from payroll_copilot.repositories.attendance_repo import AttendanceRepository

    record = AttendanceRepository(session).lop_record(employee.id, period)
    days = {a.day.isoformat(): a.status.value for a in session.scalars(select(Attendance).where(
        Attendance.employee_id == employee.id,
        Attendance.day.in_([a.date for a in change.attendance])))} if change.attendance else {}
    return {"lop": str(record.days) if record else "0", "days": days,
            "entries": _entry_ids(session, employee, period)}


def _reverse(session: Session, employee: Employee, period: PayrollPeriod,
             change: PayrollChange, before: dict[str, Any]) -> tuple[dict | None, str]:  # fmt: skip
    if change.salary is not None:
        return None, "A salary revision cannot be undone automatically - revise the salary again."
    if change.fill_month:
        return None, "Created attendance cannot be undone automatically - correct the days."
    if change.cancel_entries:
        return None, "This was an undo; repeat the original change instead of undoing it."
    undo: dict[str, Any] = {}
    if change.lop_days is not None:
        undo["lop_days"] = before["lop"]
    if change.attendance:
        missing = [a.date.isoformat() for a in change.attendance
                   if a.date.isoformat() not in before["days"]]
        if missing:
            return None, (f"Attendance for {', '.join(missing)} did not exist before - "
                          "correct it manually.")
        undo["attendance"] = [{"date": d, "status": st} for d, st in before["days"].items()]
    after = _entry_ids(session, employee, period)
    cancel = [{"kind": k, "id": i} for k in ("overtime", "adjustment")
              for i in sorted(after[k] - before["entries"][k])]
    if cancel:
        undo["cancel_entries"] = cancel
    return (undo or None), ("" if undo else "Nothing to undo")


def _undo_change(row: ActionRequest, reason: str) -> PayrollChange:
    if row.status != APPLIED:
        raise ActionConflictError("Only an applied change can be undone")
    result = row.result or {}
    if result.get("undone_by"):
        raise ActionConflictError(f"Already undone (action {result['undone_by']})")
    if not result.get("undo"):
        raise ActionConflictError(result.get("undo_note") or "This change cannot be undone")
    return PayrollChange.model_validate({**result["undo"], "reason": reason})


def undo_preview(session: Session, actor: Employee, public_id: str) -> dict[str, Any]:
    row = _get(session, public_id)
    change = _undo_change(row, "Undo preview")
    employee = session.get(Employee, row.employee_id)
    return simulate(session, actor, employee.employee_code, PayrollPeriod.parse(row.period),
                    change)  # fmt: skip


def undo(session: Session, actor: Employee, public_id: str, preview_id: str, reason: str,
         request_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Reverse an applied change with a NEW action (same preview, approval and audit)."""
    row = _get(session, public_id)
    change = _undo_change(row, f"Undo of '{row.title}': {reason.strip()}")
    employee = session.get(Employee, row.employee_id)
    out = submit(session, actor, employee.employee_code, PayrollPeriod.parse(row.period),
                 change, preview_id, "undo", request_id)  # fmt: skip
    row.result = {**(row.result or {}), "undone_by": out["id"]}
    audit.record(session, "action.undo", actor=actor, entity="action_request",
                 entity_id=row.public_id, new={"undo_action": out["id"], "reason": reason.strip()},
                 request_id=request_id)  # fmt: skip
    session.flush()
    return out
