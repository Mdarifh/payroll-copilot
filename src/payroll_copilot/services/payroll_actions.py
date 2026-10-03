"""Apply payroll changes that the user previewed and confirmed (LOP, overtime, one-off pay,
salary revision), cancel entries, and recalculate a month.

Flow for every write:
    preview (simulation, nothing saved) -> user confirms with a reason -> apply here:
    the preview id is checked against today's data, the change goes through the same
    validated services the preview used, affected processed payslips are recalculated,
    validation / approval of those months is reset, and everything is audited.
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import (
    NotFoundError,
    PayrollCopilotError,
    PermissionDeniedError,
)
from payroll_copilot.db.models import (
    BatchItem,
    Employee,
    OvertimeEntry,
    PayAdjustment,
    PayrollBatch,
    PayrollRun,
)
from payroll_copilot.domain.enums import BatchItemStatus, BatchStatus, PayrollRunStatus
from payroll_copilot.services import audit
from payroll_copilot.services.pay_adjustments import AdjustmentError, cancel
from payroll_copilot.services.payslip import process_payroll
from payroll_copilot.services.simulation import PayrollChange, apply_change, simulate

RESETTABLE = ("VALIDATED", "VALIDATION_FAILED", "APPROVED")


class StalePreviewError(PayrollCopilotError):
    """The data changed after the preview: preview again before applying."""


def _employee(session: Session, code: str) -> Employee:
    e = session.scalar(select(Employee).where(Employee.employee_code == code.upper()))
    if e is None:
        raise NotFoundError(f"Employee {code} not found")
    return e


def _recalculate(session: Session, actor: Employee, employee: Employee, months: list[str],
                 request_id: str | None) -> tuple[list[str], list[str]]:  # fmt: skip
    """Re-run processed, unlocked payslips of ``months``; reset their batch approvals."""
    runs = list(session.scalars(select(PayrollRun).where(
        PayrollRun.employee_id == employee.id, PayrollRun.period.in_(months))
        .order_by(PayrollRun.period)))  # fmt: skip
    done, skipped = [], []
    for run in runs:
        if run.status is PayrollRunStatus.LOCKED:
            skipped.append(f"{run.period} is finalized (locked) - not recalculated")
            continue
        process_payroll(session, employee, PayrollPeriod.parse(run.period), run_by=actor,
                        request_id=request_id)  # fmt: skip
        done.append(run.period)
    for period in done:
        _reset_approval(session, actor, period, f"payslip of {employee.employee_code} changed")
    return done, skipped


def _reset_approval(session: Session, actor: Employee, period: str, why: str) -> None:
    """A validated or approved month must be validated again after a payslip changed."""
    batches = list(session.scalars(select(PayrollBatch).where(
        PayrollBatch.period == period, PayrollBatch.stage.in_(RESETTABLE))))
    for b in batches:
        old = b.stage
        b.stage, b.validated_at, b.approved_at, b.approved_by_id = None, None, None, None
        audit.record(session, "batch.stage_reset", actor=actor, entity="payroll_batch",
                     entity_id=b.batch_id, old={"stage": old},
                     new={"stage": None, "reason": why})  # fmt: skip


def apply(session: Session, actor: Employee, employee_code: str, period: PayrollPeriod,
          change: PayrollChange, preview: str,
          request_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    if len(change.reason.strip()) < 5 or change.reason.strip() == "What-if preview":
        raise AdjustmentError("A reason (at least 5 characters) is required to apply a change")
    employee = _employee(session, employee_code)
    # the preview the user confirmed must still match today's data
    current = simulate(session, actor, employee.employee_code, period, change)
    if current["preview_id"] != preview:
        raise StalePreviewError("Payroll data changed after the preview. Please preview the "
                                "change again and confirm the new numbers.")  # fmt: skip
    apply_change(session, actor, employee, period, change, request_id)
    months = {str(period)}
    if change.salary is not None:  # a revision affects its month and every later one
        later = session.scalars(select(PayrollRun.period).where(
            PayrollRun.employee_id == employee.id,
            PayrollRun.period >= str(PayrollPeriod.from_date(change.salary.effective_from))))
        months |= set(later)
    recalculated, skipped = _recalculate(session, actor, employee, sorted(months), request_id)
    audit.record(session, "action.apply", actor=actor, entity="employee",
                 entity_id=employee.employee_code,
                 new={"period": str(period), "change": change.model_dump(mode="json"),
                      "preview_id": preview, "net_change": current["net_change"],
                      "recalculated": recalculated, "reason": change.reason},
                 request_id=request_id)  # fmt: skip
    session.flush()
    return {**current, "saved": True, "recalculated": recalculated, "not_recalculated": skipped}


def cancel_entry(session: Session, actor: Employee, kind: str, entry_id: int, reason: str,
                 request_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    model = {"overtime": OvertimeEntry, "adjustment": PayAdjustment}.get(kind)
    if model is None:
        raise NotFoundError(f"Unknown entry type {kind}")
    row = session.get(model, entry_id)
    if row is None:
        raise NotFoundError(f"{kind.title()} entry {entry_id} not found")
    from payroll_copilot.auth.permissions import change_perm, has

    if not has(actor, change_perm(kind)):
        raise PermissionDeniedError("Your role cannot cancel this kind of entry")
    employee = session.get(Employee, row.employee_id)
    cancel(session, actor, row, reason)
    recalculated, skipped = _recalculate(session, actor, employee, [row.period], request_id)
    return {"cancelled": entry_id, "kind": kind, "employee_code": employee.employee_code,
            "period": row.period, "recalculated": recalculated, "not_recalculated": skipped}


def recalculate_month(session: Session, actor: Employee, period: PayrollPeriod, reason: str,
                      department: str | None = None,
                      work_state: str | None = None) -> PayrollBatch:  # fmt: skip
    """Queue a re-run of every processed, unlocked payslip of the month (e.g. after a rule
    or data fix). The worker processes it like any batch: progress, failures, retry."""
    from uuid import uuid4

    from payroll_copilot.services.batch_service import BatchConflictError
    from payroll_copilot.services.payroll_source import normalise_state

    if len((reason or "").strip()) < 5:
        raise AdjustmentError("A reason (at least 5 characters) is required to recalculate")
    state = normalise_state(work_state)
    busy = session.scalar(select(PayrollBatch).where(
        PayrollBatch.period == str(period),
        PayrollBatch.status.in_([BatchStatus.REVIEW, BatchStatus.QUEUED, BatchStatus.RUNNING])))
    if busy is not None:
        raise BatchConflictError(f"A batch for {period.label} is still in "
                                 f"{busy.status.value.lower()} - finish or roll it back first")
    stmt = (select(PayrollRun.employee_id).join(Employee, Employee.id == PayrollRun.employee_id)
            .where(PayrollRun.period == str(period),
                   PayrollRun.status == PayrollRunStatus.PROCESSED))  # fmt: skip
    if department:
        stmt = stmt.where(Employee.department == department)
    if state:
        stmt = stmt.where(Employee.work_state == state)
    ids = list(session.scalars(stmt))
    if not ids:
        raise AdjustmentError(f"No processed, unlocked payslips in {period.label} to recalculate")
    filters = {k: v for k, v in {"department": department, "work_state": state}.items() if v}
    batch = PayrollBatch(batch_id=uuid4().hex, period=str(period), created_by_id=actor.id,
                         filters={**filters, "recalculate": True}, status=BatchStatus.QUEUED,
                         total=len(ids), ok_count=len(ids), lop_mode="KEEP",
                         reason=reason.strip())  # fmt: skip
    session.add(batch)
    session.flush()
    session.add_all(BatchItem(batch_pk=batch.id, employee_id=i, status=BatchItemStatus.PENDING)
                    for i in ids)  # fmt: skip
    _reset_approval(session, actor, str(period), "month recalculated")
    session.execute(update(PayrollBatch).where(PayrollBatch.id == batch.id)
                    .values(run_requested_at=batch.created_at))  # fmt: skip
    audit.record(session, "batch.recalculate", actor=actor, entity="payroll_batch",
                 entity_id=batch.batch_id,
                 new={"period": str(period), "employees": len(ids), "filters": filters,
                      "reason": reason.strip()}, request_id=batch.batch_id)  # fmt: skip
    session.flush()
    return batch


def recalculate_employee(session: Session, actor: Employee, employee_code: str,
                         period: PayrollPeriod, reason: str,
                         request_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Re-run one processed, unlocked payslip (the "Fix" of an audit finding)."""
    if len((reason or "").strip()) < 5:
        raise AdjustmentError("A reason (at least 5 characters) is required to recalculate")
    employee = _employee(session, employee_code)
    run = session.scalar(select(PayrollRun).where(PayrollRun.employee_id == employee.id,
                                                  PayrollRun.period == str(period)))
    if run is None:
        raise NotFoundError(f"No payslip for {employee.employee_code} in {period.label}")
    old_net = run.net_pay
    done, skipped = _recalculate(session, actor, employee, [str(period)], request_id)
    if skipped:
        raise AdjustmentError(skipped[0])
    audit.record(session, "payroll.recalculate", actor=actor, entity="payroll_run",
                 entity_id=run.id, old={"net_pay": old_net},
                 new={"employee_code": employee.employee_code, "period": str(period),
                      "net_pay": run.net_pay, "reason": reason.strip()},
                 request_id=request_id)  # fmt: skip
    return {"employee_code": employee.employee_code, "period": str(period),
            "old_net": str(old_net), "new_net": str(run.net_pay), "version": run.version}


MAX_SELECTED = 1000


def run_selected(session: Session, actor: Employee, period: PayrollPeriod, codes: list[str],
                 reason: str, lop_days: dict[str, Any] | None = None) -> PayrollBatch:  # fmt: skip
    """Run payroll for the chosen employees only (a queued batch, processed by the worker):
    not yet processed -> calculated; processed and unlocked -> recalculated; locked or not
    employed in the month -> refused with the names.

    ``lop_days`` ({code: days}) sets LOP for some of them first, like the LOP review of a
    normal run (validated and audited by lop_service.set_lop with the same reason); others
    keep the LOP already decided."""
    from uuid import uuid4

    from payroll_copilot.services.batch_service import BatchConflictError

    if len((reason or "").strip()) < 5:
        raise AdjustmentError("A reason (at least 5 characters) is required")
    wanted = list(dict.fromkeys(c.strip().upper() for c in codes if c and c.strip()))
    if not wanted:
        raise AdjustmentError("Select at least one employee")
    if len(wanted) > MAX_SELECTED:
        raise AdjustmentError(f"Select at most {MAX_SELECTED} employees at a time")
    employees = list(session.scalars(select(Employee).where(Employee.employee_code.in_(wanted))))
    found = {e.employee_code for e in employees}
    if missing := [c for c in wanted if c not in found]:
        raise NotFoundError(f"Unknown employee(s): {', '.join(missing)}")
    outside = [e.employee_code for e in employees
               if e.doj > period.end or (e.lwd and e.lwd < period.start)]
    if outside:
        raise AdjustmentError(f"Not employed in {period.label}: {', '.join(outside)}")
    locked = session.scalars(select(Employee.employee_code).join(
        PayrollRun, PayrollRun.employee_id == Employee.id).where(
        PayrollRun.period == str(period), PayrollRun.status == PayrollRunStatus.LOCKED,
        Employee.employee_code.in_(wanted))).all()  # fmt: skip
    if locked:
        raise AdjustmentError(f"Payroll {period.label} is finalized (locked) for "
                              f"{', '.join(locked)} - reopen the month first")  # fmt: skip
    busy = session.scalar(select(PayrollBatch).where(
        PayrollBatch.period == str(period),
        PayrollBatch.status.in_([BatchStatus.REVIEW, BatchStatus.QUEUED, BatchStatus.RUNNING])))
    if busy is not None:
        raise BatchConflictError(f"A batch for {period.label} is still in "
                                 f"{busy.status.value.lower()} - finish or roll it back first")
    lop_changes: dict[str, str] = {}
    if lop_days:
        from decimal import Decimal, InvalidOperation

        from payroll_copilot.services.lop_service import set_lop

        by_code = {e.employee_code: e for e in employees}
        for code, value in lop_days.items():
            employee = by_code.get(str(code).strip().upper())
            if employee is None:
                raise AdjustmentError(f"LOP given for {code}, who is not selected")
            try:
                days = Decimal(str(value))
            except InvalidOperation:
                raise AdjustmentError(f"LOP days for {code} must be a number") from None
            try:
                set_lop(session, actor, employee, period, days, reason)
            except AdjustmentError as exc:
                raise AdjustmentError(f"{employee.full_name} ({employee.employee_code}): "
                                      f"{exc}") from exc  # fmt: skip
            lop_changes[employee.employee_code] = str(days)
    batch = PayrollBatch(batch_id=uuid4().hex, period=str(period), created_by_id=actor.id,
                         filters={"employees": sorted(found), "recalculate": True},
                         status=BatchStatus.QUEUED, total=len(employees),
                         ok_count=len(employees), lop_mode="KEEP", reason=reason.strip())
    session.add(batch)
    session.flush()
    session.add_all(BatchItem(batch_pk=batch.id, employee_id=e.id,
                              status=BatchItemStatus.PENDING) for e in employees)  # fmt: skip
    _reset_approval(session, actor, str(period), "payroll run for selected employees")
    session.execute(update(PayrollBatch).where(PayrollBatch.id == batch.id)
                    .values(run_requested_at=batch.created_at))  # fmt: skip
    audit.record(session, "batch.run_selected", actor=actor, entity="payroll_batch",
                 entity_id=batch.batch_id,
                 new={"period": str(period), "employees": sorted(found),
                      "lop_days": lop_changes, "reason": reason.strip()},
                 request_id=batch.batch_id)  # fmt: skip
    session.flush()
    return batch
