"""Audit Timeline: every recorded change as a readable event, newest first.

Built only from audit_log rows (nothing is inferred). Chat questions and logins are left
out unless asked for, so the timeline shows payroll changes.
"""

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, aliased

from payroll_copilot.db.base import iso_utc
from payroll_copilot.db.models import AuditLog, Employee

LABELS = {
    "payroll.run": "Payslip calculated", "payroll.recalculate": "Payslip recalculated",
    "lop.update": "LOP updated", "attendance.update": "Attendance corrected",
    "attendance.bulk_create": "Attendance created for the month",
    "attendance.leave": "Approved leave written to attendance",
    "leave.apply": "Leave applied", "leave.approve": "Leave approved",
    "leave.reject": "Leave rejected", "leave.cancel": "Leave request withdrawn",
    "action.undo": "Change undone", "report.download": "Report downloaded",
    "overtime.add": "Overtime added", "overtime.cancel": "Overtime cancelled",
    "adjustment.add": "One-off pay added", "adjustment.cancel": "One-off pay cancelled",
    "salary.revise": "Salary revised", "action.submit": "Change requested",
    "action.approve": "Change approved", "action.reject": "Change rejected",
    "action.cancel": "Change withdrawn", "action.apply": "Change applied",
    "action.failed": "Change failed", "batch.start": "Payroll review started",
    "batch.run": "Payroll run started", "batch.run_selected": "Payroll run for selected employees",
    "batch.retry_failed": "Failed employees retried", "batch.validate": "Payroll validated",
    "batch.approve": "Payroll approved", "batch.finalize": "Payroll finalized",
    "batch.rollback": "Payroll rolled back", "batch.recalculate": "Month recalculated",
    "batch.reopen": "Month reopened", "batch.stage_reset": "Validation reset after a change",
    "ai_audit.start": "AI audit run", "settings.update": "Setting changed",
    "settings.change_approval": "Change approval switched",
    "user.update": "User role / login changed",
    "knowledge.upload": "Policy document added", "knowledge.remove": "Policy document removed",
    "payslip.download": "Payslip downloaded", "auth.login": "Logged in",
    "auth.login_failed": "Failed login", "chat.ask": "Chat question", "chat.tool": "Chat tool used",
    "chat.route": "Chat intent",
}  # fmt: skip
NOISE = ("chat.", "auth.")


def _money(value: Any) -> str:
    try:
        return f"₹{Decimal(str(value)):,.0f}"
    except (InvalidOperation, ValueError):
        return str(value)


def _summary(action: str, old: dict | None, new: dict | None) -> str:
    old, new = old or {}, new or {}
    period = new.get("period", "")
    if action == "lop.update":
        return f"LOP {old.get('lop_days', 0)} → {new.get('lop_days')} days {period}".strip()
    if action in ("payroll.run", "payroll.recalculate"):
        net = new.get("net_pay")
        if not net:
            return period
        return f"{period} payslip v{new.get('version', '')}: net pay {_money(net)}"
    if action == "overtime.add":
        return f"{new.get('formula', '')} on {new.get('work_date', '')}"
    if action == "adjustment.add":
        return f"{new.get('kind', '').title()} {new.get('label') or new.get('head_code')} " \
               f"{_money(new.get('amount'))} ({period})"
    if action == "attendance.update":
        days = ", ".join(f"{c['date']}: {c['old']} → {c['new']}"
                         for c in new.get("changes", [])[:3])
        return f"{period}: {days}"
    if action == "salary.revise":
        return f"Monthly gross {_money(new.get('monthly_gross'))} from {new.get('effective_from')}"
    if action.startswith("action."):
        return new.get("title") or new.get("error") or new.get("note") or ""
    if action == "batch.run_selected":
        return f"{period}: {', '.join(new.get('employees', [])[:5])}"
    if action == "batch.validate":
        return f"{new.get('errors', 0)} errors, {new.get('warnings', 0)} warnings"
    if action == "user.update":
        return ", ".join(f"{k}: {old.get(k)} → {v}" for k, v in new.items() if k != "reason")
    if action == "settings.change_approval":
        return "ON" if new.get("enabled") else "OFF"
    if action == "chat.ask":
        return str(new.get("message", ""))[:120]
    return str(period or new.get("title") or "")


def timeline(session: Session, employee_code: str | None = None, action: str | None = None,
             actor_code: str | None = None, date_from: date | None = None,
             date_to: date | None = None, include_system: bool = False, page: int = 1,
             page_size: int = 30) -> dict[str, Any]:  # fmt: skip
    actor = aliased(Employee)
    stmt = select(AuditLog, actor).outerjoin(actor, actor.id == AuditLog.actor_id)
    if not include_system:
        stmt = stmt.where(~AuditLog.action.like("chat.%"), ~AuditLog.action.like("auth.%"))
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    if employee_code:
        code = employee_code.strip().upper()
        stmt = stmt.where(or_(AuditLog.entity_id == code,
                              cast(AuditLog.new_value, String).like(f'%"{code}"%'),
                              cast(AuditLog.old_value, String).like(f'%"{code}"%')))
    if actor_code:
        stmt = stmt.where(actor.employee_code == actor_code.strip().upper())
    if date_from:
        stmt = stmt.where(AuditLog.at >= datetime.combine(date_from, time.min))
    if date_to:
        stmt = stmt.where(AuditLog.at <= datetime.combine(date_to, time.max))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.execute(stmt.order_by(AuditLog.id.desc())
                           .offset((page - 1) * page_size).limit(page_size)).all()  # fmt: skip
    items = []
    for log, who in rows:
        new = log.new_value if isinstance(log.new_value, dict) else {}
        items.append({
            "id": log.id, "at": iso_utc(log.at), "action": log.action,
            "label": LABELS.get(log.action, log.action),
            "group": log.action.split(".")[0],
            "actor": who.full_name if who else "System", "actor_role": log.actor_role,
            "entity": log.entity, "entity_id": log.entity_id,
            "employee": new.get("employee_code") or (log.entity_id if log.entity == "employee"
                                                     else None),
            "summary": _summary(log.action, log.old_value if isinstance(log.old_value, dict)
                                else None, new),
            "reason": new.get("reason"), "old": log.old_value, "new": log.new_value,
            "request_id": log.request_id,
        })  # fmt: skip
    # action groups for the filter (portable: split in Python, not with SQL string functions)
    groups = {a.split(".")[0] for a in session.scalars(select(AuditLog.action).distinct())}
    return {"total": total, "page": page, "pages": max(1, -(-total // page_size)),
            "items": items, "groups": sorted(g for g in groups if g and not (
                not include_system and f"{g}." in NOISE))}  # fmt: skip
