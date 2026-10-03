"""Overview dashboard: read-only numbers aggregated from the database.

Nothing is calculated for display here beyond sums of stored, processed payroll;
React only formats. HR sees the company; everyone else sees their own summary.
"""

from collections import Counter
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from payroll_copilot.auth.permissions import is_staff
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.base import iso_utc
from payroll_copilot.db.models import AuditLog, Employee, PayrollBatch, PayrollRun
from payroll_copilot.domain.enums import EmployeeStatus, Role

ZERO = Decimal(0)
STATUTORY = {"EE_PF": "PF", "EE_ESIC": "ESIC", "PT": "PT", "TDS": "TDS"}
TREND_MONTHS = 6
# audit actions worth showing on the dashboard (chat.* and auth.* are noise there)
ACTIVITY_PREFIXES = ("payroll.", "batch.", "lop.", "salary.", "payslip.", "action.",
                     "overtime.", "adjustment.")


def _company(session: Session) -> dict[str, Any]:
    status = Counter(dict(session.execute(
        select(Employee.status, func.count()).group_by(Employee.status)).all()))  # fmt: skip
    departments = session.execute(
        select(Employee.department, func.count())
        .where(Employee.status != EmployeeStatus.SEPARATED)
        .group_by(Employee.department).order_by(func.count().desc())).all()  # fmt: skip

    # money is stored as text on SQLite: never SUM it in SQL (floats) - add Decimals here
    periods = list(session.scalars(select(PayrollRun.period).distinct()
                                   .order_by(PayrollRun.period.desc()).limit(TREND_MONTHS)))
    totals: dict[str, list] = {p: [0, ZERO, ZERO, ZERO] for p in periods}
    for p, g, d, net in session.execute(
            select(PayrollRun.period, PayrollRun.gross_earnings, PayrollRun.total_deductions,
                   PayrollRun.net_pay).where(PayrollRun.period.in_(periods))):  # fmt: skip
        t = totals[p]
        t[0] += 1
        t[1] += g
        t[2] += d
        t[3] += net
    trend = [{"period": p, "label": PayrollPeriod.parse(p).label, "employees": n,
              "gross": str(g), "deductions": str(d), "net": str(net)}
             for p, (n, g, d, net) in sorted(totals.items())]  # fmt: skip

    latest = trend[-1] if trend else None
    statutory: dict[str, Decimal] = dict.fromkeys(STATUTORY.values(), ZERO)
    if latest:
        for payslip in session.scalars(select(PayrollRun.payslip).where(
                PayrollRun.period == latest["period"])):  # fmt: skip
            for line in payslip.get("deductions", []):
                label = STATUTORY.get(line["head_code"])
                if label:
                    statutory[label] += Decimal(line["amount"])

    batch = session.scalar(select(PayrollBatch).order_by(PayrollBatch.id.desc()).limit(1))
    actor = aliased(Employee)
    activity = session.execute(
        select(AuditLog, actor.full_name).outerjoin(actor, actor.id == AuditLog.actor_id)
        .where(func.substr(AuditLog.action, 1, 4).in_({p[:4] for p in ACTIVITY_PREFIXES}))
        .order_by(AuditLog.id.desc()).limit(40)).all()  # fmt: skip
    activity = [(a, n) for a, n in activity if a.action.startswith(ACTIVITY_PREFIXES)][:8]

    return {
        "scope": "company",
        "headcount": {"active": status[EmployeeStatus.ACTIVE],
                      "notice": status[EmployeeStatus.NOTICE],
                      "separated": status[EmployeeStatus.SEPARATED]},
        "departments": [{"name": d or "Unassigned", "count": n} for d, n in departments],
        "latest_payroll": latest,
        "statutory": {k: str(v) for k, v in statutory.items()},
        "trend": trend,
        "latest_batch": batch and {
            "batch_id": batch.batch_id, "period": batch.period, "status": batch.status.value,
            "stage": batch.stage, "total": batch.total,
        },
        "pending_actions": _pending(session),
        "activity": [{"at": iso_utc(a.at), "action": a.action, "actor": n or "System",
                      "entity": a.entity, "entity_id": a.entity_id} for a, n in activity],
    }  # fmt: skip


def _pending(session: Session) -> int:
    from payroll_copilot.services.action_center import pending_count

    return pending_count(session)


def _personal(session: Session, user: Employee) -> dict[str, Any]:
    runs = session.scalars(select(PayrollRun).where(PayrollRun.employee_id == user.id)
                           .order_by(PayrollRun.period.desc()).limit(TREND_MONTHS)).all()
    team = None
    if user.role is Role.MANAGER:
        team = session.scalar(select(func.count()).select_from(Employee).where(
            Employee.manager_id == user.id, Employee.status != EmployeeStatus.SEPARATED))
    return {
        "scope": "personal",
        "team_size": team,
        "trend": [{"period": r.period, "label": PayrollPeriod.parse(r.period).label,
                   "gross": str(r.gross_earnings), "deductions": str(r.total_deductions),
                   "net": str(r.net_pay), "lop_days": str(r.lop_days)}
                  for r in reversed(runs)],
    }  # fmt: skip


def overview(session: Session, user: Employee) -> dict[str, Any]:
    return _company(session) if is_staff(user) else _personal(session, user)
