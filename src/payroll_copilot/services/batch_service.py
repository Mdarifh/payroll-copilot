"""Bulk payroll for any number of employees.

start_review  page through employees (100 at a time), find unapproved absences and store
              them as LOP review rows; every employee in scope becomes a batch item
              (the worker's job). Only a summary goes back to the chat/LLM.
update_lop    HR edits one review row (apply / days); totals are recomputed in the DB.
request_run   REVIEW -> QUEUED in ONE conditional UPDATE: a double click or a second
              request finds the batch no longer in REVIEW and gets a conflict error.
              LOP is written for the checked rows (audited per employee, with batch_id).
retry_failed  finished batch: FAILED items -> PENDING again, batch -> QUEUED.
The background worker (batch_worker) processes the queued items.
"""

import csv
import io
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from payroll_copilot.core.config import LopBase, LopDivisor, get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import (
    ConfigurationError,
    NotFoundError,
    PayrollCopilotError,
)
from payroll_copilot.db.base import iso_utc
from payroll_copilot.db.models import (
    BatchItem,
    BatchLopItem,
    Employee,
    LopRecord,
    PayrollBatch,
)
from payroll_copilot.domain.enums import FINISHED_BATCH, BatchItemStatus, BatchStatus
from payroll_copilot.repositories.attendance_repo import AttendanceRepository
from payroll_copilot.repositories.salary_repo import SalaryRepository
from payroll_copilot.services import audit
from payroll_copilot.services.payroll_source import (
    LocalPayrollSource,
    PayrollSource,
    normalise_state,
)
from payroll_copilot.services.reliability import RateLimiter

ZERO = Decimal(0)
HALF = Decimal("0.5")


class BatchConflictError(PayrollCopilotError):
    """The batch is not in a state that allows this (already running, finished...)."""


def now() -> datetime:
    return datetime.now(UTC)


def lop_dates_for(absent_dates: list[str], lop_days: Decimal) -> list[dict[str, str]]:
    """Put ``lop_days`` on the absence dates, earliest first (a half day takes 0.5)."""
    result, remaining = [], lop_days
    for day in sorted(absent_dates):
        if remaining <= 0:
            break
        take = min(Decimal(1), remaining)
        result.append({"date": day, "days": str(take)})
        remaining -= take
    return result


def deduction(per_day: Decimal, days: Decimal) -> Decimal:
    return (per_day * days).quantize(Decimal(1), rounding=ROUND_HALF_UP)


def get_batch(session: Session, batch_id: str) -> PayrollBatch:
    batch = session.scalar(select(PayrollBatch).where(PayrollBatch.batch_id == batch_id))
    if batch is None:
        raise NotFoundError(f"Batch {batch_id} not found")
    return batch


# ---------------------------------------------------------------------- review


def _per_day(session: Session, employee_id: int, period: PayrollPeriod) -> tuple[Decimal, ...]:
    """(base amount, divisor, per-day rate) from the LOP policy in config."""
    settings = get_settings()
    assignments = SalaryRepository(session).assignments_between(
        employee_id, period.start, period.end)  # fmt: skip
    if not assignments:
        raise NotFoundError(f"No salary structure in {period}")
    latest = assignments[-1]
    base = latest.monthly_gross if settings.lop_base is LopBase.GROSS else latest.amount("BASIC")
    if settings.lop_divisor is LopDivisor.FIXED_30:
        divisor = Decimal(30)
    elif settings.lop_divisor is LopDivisor.CALENDAR:
        divisor = Decimal(period.calendar_days)
    else:
        record = AttendanceRepository(session).get(employee_id, period)
        divisor = record.working_days if record else Decimal(period.calendar_days)
    per_day = (base / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return base, divisor, per_day


def start_review(
    session: Session, hr: Employee, period: PayrollPeriod, department: str | None = None,
    work_state: str | None = None, source: PayrollSource | None = None,
) -> PayrollBatch:  # fmt: skip
    """Create a batch: all employees in scope + LOP review rows for unapproved absences."""
    settings = get_settings()
    if problems := settings.lop_issues():
        raise ConfigurationError("; ".join(problems))
    try:
        state = normalise_state(work_state)
    except ValueError as exc:
        raise PayrollCopilotError(str(exc)) from exc
    source = source or LocalPayrollSource(session, RateLimiter(settings.batch_source_rate),
                                          attempts=settings.batch_max_attempts)  # fmt: skip
    batch = PayrollBatch(
        batch_id=uuid4().hex, period=str(period), created_by_id=hr.id,
        filters={k: v for k, v in {"department": department, "work_state": state}.items() if v},
    )  # fmt: skip
    session.add(batch)
    session.flush()

    total = ok = review = processed_before = 0
    page = 0
    while True:
        rows = source.employees_page(period, department, state, page, settings.batch_page_size)
        if not rows:
            break
        ids = [r.id for r in rows]
        absences = source.absences(ids, period)
        decided = source.lop_decided(ids, period)
        done = source.processed(ids, period)
        for row in rows:
            total += 1
            if row.id in done:  # idempotent: never process a month twice
                processed_before += 1
                session.add(BatchItem(batch_pk=batch.id, employee_id=row.id,
                                      status=BatchItemStatus.SKIPPED,
                                      error="already processed for this month"))  # fmt: skip
                continue
            session.add(BatchItem(batch_pk=batch.id, employee_id=row.id))
            days = absences.get(row.id, [])
            absent = sum((d for _, d in days), ZERO)
            # no exceptions, or HR's LOP decision already covers every absence (an absence
            # marked after the decision brings the employee back to the review)
            if not days or decided.get(row.id, ZERO) >= absent:
                ok += 1
                continue
            review += 1
            base, divisor, per_day = _per_day(session, row.id, period)
            session.add(BatchLopItem(
                batch_pk=batch.id, employee_id=row.id,
                absent_dates=[day.isoformat() for day, _ in days], absent_days=absent,
                suggested_days=absent, lop_days=absent, apply=True,
                base_amount=base, divisor=divisor, per_day=per_day,
            ))  # fmt: skip
        page += 1
        session.flush()

    batch.total, batch.ok_count, batch.review_count = total, ok, review
    batch.already_processed = processed_before
    audit.record(session, "batch.start", actor=hr, entity="payroll_batch",
                 entity_id=batch.batch_id,
                 new={"period": batch.period, "filters": batch.filters, "total": total,
                      "ok": ok, "review": review, "already_processed": processed_before},
                 request_id=batch.batch_id)  # fmt: skip
    session.flush()
    return batch


def summary(batch: PayrollBatch) -> dict[str, Any]:
    return {"batch_id": batch.batch_id, "period": batch.period, "filters": batch.filters,
            "status": batch.status.value, "total": batch.total, "ok_count": batch.ok_count,
            "review_count": batch.review_count,
            "already_processed": batch.already_processed, "stage": batch.stage}  # fmt: skip


def lop_totals(session: Session, batch: PayrollBatch) -> dict[str, Any]:
    """Across ALL review rows of the batch (not only the rows on screen)."""
    rows = session.execute(select(BatchLopItem.per_day, BatchLopItem.lop_days).where(
        BatchLopItem.batch_pk == batch.id, BatchLopItem.apply.is_(True))).all()  # fmt: skip
    return {"selected": len(rows), "total_lop_days": str(sum((d for _, d in rows), ZERO)),
            "total_deduction": str(sum((deduction(p, d) for p, d in rows), ZERO))}  # fmt: skip


def _lop_row(item: BatchLopItem, employee: Employee) -> dict[str, Any]:
    return {
        "emp_id": employee.employee_code, "name": employee.full_name,
        "department": employee.department, "work_state": employee.work_state,
        "absent_dates": item.absent_dates, "absent_days": str(item.absent_days),
        "suggested_days": str(item.suggested_days), "lop_days": str(item.lop_days),
        "apply": item.apply, "per_day": str(item.per_day), "divisor": str(item.divisor),
        "deduction": str(deduction(item.per_day, item.lop_days)),
    }  # fmt: skip


def lop_items(session: Session, batch: PayrollBatch, page: int = 1, page_size: int = 20,
              search: str | None = None) -> dict[str, Any]:  # fmt: skip
    stmt = (select(BatchLopItem, Employee)
            .join(Employee, Employee.id == BatchLopItem.employee_id)
            .where(BatchLopItem.batch_pk == batch.id))  # fmt: skip
    if search and search.strip():
        like = f"%{search.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Employee.full_name).like(like),
                              func.lower(Employee.employee_code).like(like),
                              func.lower(Employee.department).like(like)))  # fmt: skip
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = session.execute(stmt.order_by(Employee.employee_code)
                           .offset((page - 1) * page_size).limit(page_size)).all()  # fmt: skip
    return {"items": [_lop_row(item, emp) for item, emp in rows], "page": page,
            "page_size": page_size, "total_items": total,
            "pages": max(1, -(-total // page_size)), "totals": lop_totals(session, batch),
            "editable": batch.status is BatchStatus.REVIEW}  # fmt: skip


def update_lop_item(session: Session, batch: PayrollBatch, emp_code: str, apply: bool | None,
                    lop_days: Decimal | None) -> dict[str, Any]:  # fmt: skip
    if batch.status is not BatchStatus.REVIEW:
        raise BatchConflictError("LOP can only be changed before payroll is run")
    row = session.execute(
        select(BatchLopItem, Employee).join(Employee, Employee.id == BatchLopItem.employee_id)
        .where(BatchLopItem.batch_pk == batch.id, Employee.employee_code == emp_code.upper())
    ).first()  # fmt: skip
    if row is None:
        raise NotFoundError(f"{emp_code} is not in this batch's LOP review")
    item, employee = row
    if lop_days is not None:
        if lop_days < 0 or lop_days > item.absent_days or lop_days % HALF:
            raise PayrollCopilotError(
                f"LOP days must be between 0 and {item.absent_days}, in steps of 0.5")
        item.lop_days = lop_days
    if apply is not None:
        item.apply = apply
    session.flush()
    return {"item": _lop_row(item, employee), "totals": lop_totals(session, batch)}


# ---------------------------------------------------------------------- run


def request_run(session: Session, batch_id: str, actor: Employee, mode: str,
                reason: str | None) -> PayrollBatch:  # fmt: skip
    """Apply LOP (mode APPLY) or none (SKIP_ALL), then queue payroll for the batch."""
    if mode not in ("APPLY", "SKIP_ALL"):
        raise PayrollCopilotError("mode must be APPLY or SKIP_ALL")
    batch = get_batch(session, batch_id)
    reason = (reason or "").strip()
    applying = mode == "APPLY" and session.scalar(select(func.count()).select_from(BatchLopItem)
        .where(BatchLopItem.batch_pk == batch.id, BatchLopItem.apply.is_(True),
               BatchLopItem.lop_days > 0))  # fmt: skip
    if applying and len(reason) < 5:
        raise PayrollCopilotError("A reason (at least 5 characters) is required to apply LOP")

    # the one place that decides who runs the batch: only one request can win this UPDATE
    won = session.execute(
        update(PayrollBatch)
        .where(PayrollBatch.id == batch.id, PayrollBatch.status == BatchStatus.REVIEW)
        .values(status=BatchStatus.QUEUED, run_requested_at=now(), lop_mode=mode,
                reason=reason or None, runs=PayrollBatch.runs + 1)
        .execution_options(synchronize_session=False)
    ).rowcount  # fmt: skip
    if won != 1:
        session.refresh(batch)
        raise BatchConflictError(f"Batch is already {batch.status.value.lower()} - "
                                 "payroll was not started again")  # fmt: skip
    session.refresh(batch)

    if mode == "APPLY":
        items = session.scalars(select(BatchLopItem).where(
            BatchLopItem.batch_pk == batch.id, BatchLopItem.apply.is_(True)))  # fmt: skip
        for item in items:
            record = session.scalar(select(LopRecord).where(
                LopRecord.employee_id == item.employee_id, LopRecord.period == batch.period))
            old = None if record is None else {"lop_days": record.days, "reason": record.reason}
            dates = lop_dates_for(item.absent_dates, item.lop_days)
            if record is None:
                record = LopRecord(employee_id=item.employee_id, period=batch.period,
                                   days=item.lop_days, lop_dates=dates, reason=reason,
                                   updated_by_id=actor.id)  # fmt: skip
                session.add(record)
            else:
                record.days, record.reason, record.updated_by_id = item.lop_days, reason, actor.id
                record.lop_dates = dates
            session.execute(update(BatchItem).where(
                BatchItem.batch_pk == batch.id, BatchItem.employee_id == item.employee_id)
                .values(lop_days=item.lop_days))  # fmt: skip
            session.flush()
            audit.record(session, "lop.update", actor=actor, entity="lop_record",
                         entity_id=record.id, old=old,
                         new={"employee_id": item.employee_id, "period": batch.period,
                              "lop_days": item.lop_days, "lop_dates": dates, "reason": reason,
                              "batch_id": batch.batch_id},
                         request_id=batch.batch_id)  # fmt: skip
    audit.record(session, "batch.run", actor=actor, entity="payroll_batch",
                 entity_id=batch.batch_id, new={"mode": mode, "reason": reason},
                 request_id=batch.batch_id)  # fmt: skip
    session.flush()
    return batch


def retry_failed(session: Session, batch_id: str, actor: Employee) -> PayrollBatch:
    batch = get_batch(session, batch_id)
    won = session.execute(
        update(PayrollBatch)
        .where(PayrollBatch.id == batch.id,
               PayrollBatch.status == BatchStatus.COMPLETED_WITH_ERRORS)
        .values(status=BatchStatus.QUEUED, finished_at=None, runs=PayrollBatch.runs + 1)
        .execution_options(synchronize_session=False)
    ).rowcount  # fmt: skip
    if won != 1:
        session.refresh(batch)
        raise BatchConflictError(f"Only a batch that finished with errors can be retried "
                                 f"(this one is {batch.status.value.lower()})")  # fmt: skip
    failed = session.execute(
        update(BatchItem)
        .where(BatchItem.batch_pk == batch.id, BatchItem.status == BatchItemStatus.FAILED)
        .values(status=BatchItemStatus.PENDING, error=None)
        .execution_options(synchronize_session=False)
    ).rowcount  # fmt: skip
    audit.record(session, "batch.retry_failed", actor=actor, entity="payroll_batch",
                 entity_id=batch.batch_id, new={"requeued": failed},
                 request_id=batch.batch_id)  # fmt: skip
    session.refresh(batch)
    return batch


# ---------------------------------------------------------------------- progress


def status(session: Session, batch: PayrollBatch) -> dict[str, Any]:
    counts = dict(session.execute(
        select(BatchItem.status, func.count()).where(BatchItem.batch_pk == batch.id)
        .group_by(BatchItem.status)).all())  # fmt: skip
    total = sum(counts.values())
    pending = counts.get(BatchItemStatus.PENDING, 0)
    lop_applied = session.scalar(select(func.count()).select_from(BatchItem).where(
        BatchItem.batch_pk == batch.id, BatchItem.lop_days > 0))  # fmt: skip
    return {
        **summary(batch),
        "processed": total - pending, "pending": pending,
        "done": counts.get(BatchItemStatus.DONE, 0),
        "skipped": counts.get(BatchItemStatus.SKIPPED, 0),
        "failed": counts.get(BatchItemStatus.FAILED, 0),
        "lop_applied": lop_applied,
        "percent": round(100 * (total - pending) / total) if total else 100,
        "finished": batch.status in FINISHED_BATCH,
        "can_retry_failed": batch.status is BatchStatus.COMPLETED_WITH_ERRORS,
        "started_at": iso_utc(batch.started_at),
        "finished_at": iso_utc(batch.finished_at),
    }  # fmt: skip


def report_csv(session: Session, batch: PayrollBatch) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["batch_id", "period", "employee_code", "employee_name", "department",
                     "status", "lop_days_applied", "net_pay", "attempts", "error"])  # fmt: skip
    rows = session.execute(
        select(BatchItem, Employee).join(Employee, Employee.id == BatchItem.employee_id)
        .where(BatchItem.batch_pk == batch.id).order_by(Employee.employee_code))  # fmt: skip
    for item, employee in rows:
        writer.writerow([batch.batch_id, batch.period, employee.employee_code,
                         employee.full_name, employee.department, item.status.value,
                         item.lop_days if item.lop_days is not None else "",
                         item.net_pay if item.net_pay is not None else "",
                         item.attempts, item.error or ""])  # fmt: skip
    return buffer.getvalue()


# ---------------------------------------------------------------------- lifecycle


def _calculated(batch: PayrollBatch) -> None:
    if batch.status not in (BatchStatus.COMPLETED, BatchStatus.COMPLETED_WITH_ERRORS):
        raise BatchConflictError("Calculate payroll first (the batch has not finished running)")


def validate(session: Session, batch_id: str, actor: Employee) -> dict[str, Any]:
    """Run every check for the employees of the batch; an ERROR blocks approval."""
    from payroll_copilot.db.models import ValidationResult
    from payroll_copilot.services.payroll_validation import ERROR, Check, summarise, validate_month

    batch = get_batch(session, batch_id)
    _calculated(batch)
    if batch.stage == "FINALIZED":
        raise BatchConflictError("Payroll is finalized - it cannot be validated again")
    employees = list(session.scalars(
        select(Employee).join(BatchItem, BatchItem.employee_id == Employee.id)
        .where(BatchItem.batch_pk == batch.id).order_by(Employee.employee_code)))  # fmt: skip
    checks = validate_month(session, PayrollPeriod.parse(batch.period), employees)
    by_id = {e.id: e.employee_code for e in employees}
    failed = session.scalars(select(BatchItem).where(
        BatchItem.batch_pk == batch.id, BatchItem.status == BatchItemStatus.FAILED))
    checks += [Check(ERROR, "PAYROLL_FAILED", f"Payroll failed: {item.error}", item.employee_id,
                     by_id.get(item.employee_id)) for item in failed]  # fmt: skip
    session.execute(ValidationResult.__table__.delete().where(
        ValidationResult.batch_pk == batch.id))  # fmt: skip
    session.add_all(ValidationResult(batch_pk=batch.id, employee_id=c.employee_id,
                                     severity=c.severity, code=c.code, message=c.message)
                    for c in checks)  # fmt: skip
    counts = summarise(checks)
    batch.stage = "VALIDATED" if counts["errors"] == 0 else "VALIDATION_FAILED"
    batch.validated_at = now()
    audit.record(session, "batch.validate", actor=actor, entity="payroll_batch",
                 entity_id=batch.batch_id, new=counts, request_id=batch.batch_id)  # fmt: skip
    session.flush()
    return {**counts, "stage": batch.stage}


def validation_results(session: Session, batch: PayrollBatch, severity: str | None = None,
                       page: int = 1, page_size: int = 50) -> dict[str, Any]:  # fmt: skip
    from payroll_copilot.db.models import ValidationResult

    stmt = (select(ValidationResult, Employee.employee_code, Employee.full_name)
            .outerjoin(Employee, Employee.id == ValidationResult.employee_id)
            .where(ValidationResult.batch_pk == batch.id))  # fmt: skip
    if severity:
        stmt = stmt.where(ValidationResult.severity == severity.upper())
    order = {"ERROR": 0, "WARNING": 1, "PASS": 2}
    rows = sorted(session.execute(stmt).all(),
                  key=lambda r: (order.get(r[0].severity, 3), r[1] or "", r[0].id))
    total = len(rows)
    rows = rows[(page - 1) * page_size: page * page_size]
    counts = dict(session.execute(
        select(ValidationResult.severity, func.count()).where(
            ValidationResult.batch_pk == batch.id).group_by(ValidationResult.severity)).all())
    return {"stage": batch.stage, "total": total, "page": page,
            "counts": {"errors": counts.get("ERROR", 0), "warnings": counts.get("WARNING", 0),
                       "passed": counts.get("PASS", 0)},
            "items": [{"severity": v.severity, "code": v.code, "message": v.message,
                       "employee_code": code, "employee_name": name}
                      for v, code, name in rows]}  # fmt: skip


def approve(session: Session, batch_id: str, actor: Employee) -> PayrollBatch:
    from payroll_copilot.core.exceptions import PermissionDeniedError
    from payroll_copilot.services.company_config import get_config

    batch = get_batch(session, batch_id)
    if batch.stage != "VALIDATED":
        raise BatchConflictError("Only a validated payroll without errors can be approved")
    cfg = get_config(session, PayrollPeriod.parse(batch.period).end)
    if cfg["payroll_second_approver"] and actor.id == batch.created_by_id:
        raise PermissionDeniedError("Payroll must be approved by a different HR user than the "
                                    "one who prepared it (maker-checker)")  # fmt: skip
    batch.stage, batch.approved_by_id, batch.approved_at = "APPROVED", actor.id, now()
    audit.record(session, "batch.approve", actor=actor, entity="payroll_batch",
                 entity_id=batch.batch_id, request_id=batch.batch_id)  # fmt: skip
    session.flush()
    return batch


def finalize(session: Session, batch_id: str, actor: Employee) -> PayrollBatch:
    """Lock the month's payslips of every employee in the batch (calculated now or by an
    earlier batch of the month); locked payslips cannot be recalculated or changed."""
    from payroll_copilot.db.models import PayrollRun
    from payroll_copilot.domain.enums import PayrollRunStatus

    batch = get_batch(session, batch_id)
    if batch.stage != "APPROVED":
        raise BatchConflictError("Approve the payroll before finalizing it")
    done = select(BatchItem.employee_id).where(
        BatchItem.batch_pk == batch.id,
        BatchItem.status.in_([BatchItemStatus.DONE, BatchItemStatus.SKIPPED]))
    locked = session.execute(
        update(PayrollRun).where(PayrollRun.period == batch.period,
                                 PayrollRun.employee_id.in_(done))
        .values(status=PayrollRunStatus.LOCKED).execution_options(synchronize_session=False)
    ).rowcount  # fmt: skip
    batch.stage, batch.finalized_by_id, batch.finalized_at = "FINALIZED", actor.id, now()
    audit.record(session, "batch.finalize", actor=actor, entity="payroll_batch",
                 entity_id=batch.batch_id, new={"locked_payslips": locked},
                 request_id=batch.batch_id)  # fmt: skip
    session.flush()
    return batch


def rollback(session: Session, batch_id: str, actor: Employee, reason: str) -> PayrollBatch:
    """Undo a calculated, not finalized run: its payslips are deleted and the batch goes
    back to review. LOP decisions and the audit trail are kept."""
    from payroll_copilot.db.models import PayrollRun

    batch = get_batch(session, batch_id)
    _calculated(batch)
    if batch.stage == "FINALIZED":
        raise BatchConflictError("A finalized payroll cannot be rolled back")
    if len((reason or "").strip()) < 5:
        raise PayrollCopilotError("A reason (at least 5 characters) is required to roll back")
    done = select(BatchItem.employee_id).where(BatchItem.batch_pk == batch.id,
                                              BatchItem.status == BatchItemStatus.DONE)
    deleted = session.execute(PayrollRun.__table__.delete().where(
        PayrollRun.period == batch.period, PayrollRun.employee_id.in_(done))).rowcount
    session.execute(update(BatchItem).where(
        BatchItem.batch_pk == batch.id, BatchItem.status != BatchItemStatus.SKIPPED)
        .values(status=BatchItemStatus.PENDING, error=None, net_pay=None, processed_at=None)
        .execution_options(synchronize_session=False))  # fmt: skip
    batch.status, batch.stage = BatchStatus.REVIEW, None
    batch.finished_at = batch.validated_at = batch.approved_at = None
    batch.approved_by_id = None
    audit.record(session, "batch.rollback", actor=actor, entity="payroll_batch",
                 entity_id=batch.batch_id, new={"deleted_payslips": deleted, "reason": reason},
                 request_id=batch.batch_id)  # fmt: skip
    session.flush()
    return batch


def pf_challan(session: Session, period: PayrollPeriod) -> dict[str, Any]:
    """Monthly EPF challan (ECR totals) from processed payslips. The establishment-level
    admin minimum is applied here only, never per employee."""
    from payroll_copilot.calculations.pf_rates import rate_sets
    from payroll_copilot.db.models import PayrollRun
    from payroll_copilot.services.statutory_rates import StatutoryRateService

    totals = dict.fromkeys(("EE_PF", "ER_EPF", "EPS", "EDLI", "EPF_ADMIN"), ZERO)
    members = 0
    for run in session.scalars(select(PayrollRun).where(PayrollRun.period == str(period))):
        lines = {ln["head_code"]: Decimal(ln["amount"])
                 for ln in [*run.payslip["deductions"], *run.payslip["employer_contributions"]]}
        if lines.get("EE_PF", ZERO) > 0:
            members += 1
        for key in totals:
            totals[key] += lines.get(key, ZERO)
    rates = rate_sets(StatutoryRateService(session).rows(), period.end, period.end)[0]
    minimum = rates.admin_min if members else rates.admin_min_no_member
    admin = max(totals["EPF_ADMIN"], minimum)
    accounts = [
        {"account": "A/c 1", "name": "EPF (employee + employer share)",
         "amount": str(totals["EE_PF"] + totals["ER_EPF"])},
        {"account": "A/c 2", "name": "EPF admin charges", "amount": str(admin)},
        {"account": "A/c 10", "name": "EPS (pension)", "amount": str(totals["EPS"])},
        {"account": "A/c 21", "name": "EDLI", "amount": str(totals["EDLI"])},
        {"account": "A/c 22", "name": "EDLI admin charges", "amount": "0"},
    ]  # fmt: skip
    return {
        "period": str(period), "contributing_members": members,
        "employee_share": str(totals["EE_PF"]), "employer_epf": str(totals["ER_EPF"]),
        "admin_by_employees": str(totals["EPF_ADMIN"]), "admin_minimum": str(minimum),
        "admin_payable": str(admin), "admin_minimum_applied": admin > totals["EPF_ADMIN"],
        "accounts": accounts,
        "total": str(sum((Decimal(a["amount"]) for a in accounts), ZERO)),
    }  # fmt: skip


def reopen_month(session: Session, period: PayrollPeriod, actor: Employee,
                 reason: str) -> dict[str, Any]:  # fmt: skip
    """Unlock a finalized month (e.g. finalized by mistake or a correction is required).
    Payslips go back to PROCESSED and finalized batches back to calculated; they must be
    validated, approved and finalized again. Audited with the reason."""
    from payroll_copilot.db.models import PayrollRun
    from payroll_copilot.domain.enums import PayrollRunStatus

    if len((reason or "").strip()) < 10:
        raise PayrollCopilotError("A reason (at least 10 characters) is required to reopen a "
                                  "finalized month")  # fmt: skip
    unlocked = session.execute(
        update(PayrollRun).where(PayrollRun.period == str(period),
                                 PayrollRun.status == PayrollRunStatus.LOCKED)
        .values(status=PayrollRunStatus.PROCESSED)
        .execution_options(synchronize_session=False)).rowcount  # fmt: skip
    batches = list(session.scalars(select(PayrollBatch).where(
        PayrollBatch.period == str(period), PayrollBatch.stage == "FINALIZED")))
    if not unlocked and not batches:
        raise BatchConflictError(f"{period.label} is not finalized")
    for b in batches:
        b.stage, b.finalized_at, b.finalized_by_id = None, None, None
        b.approved_at, b.approved_by_id, b.validated_at = None, None, None
    audit.record(session, "batch.reopen", actor=actor, entity="payroll_month",
                 entity_id=str(period), new={"unlocked_payslips": unlocked,
                                             "batches": [b.batch_id for b in batches],
                                             "reason": reason.strip()})  # fmt: skip
    session.flush()
    return {"period": str(period), "unlocked": unlocked, "batches": len(batches)}
