"""Overtime and one-off pay adjustments: quote, validate, store, cancel, and feed payroll.

These are the validated backend services every write goes through (chat actions and
the UI both call them, after the user confirmed). Each write is audited. Nothing is
deleted: a correction cancels the row.
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.overtime import (
    OvertimePolicy,
    check_hours,
    overtime_amount,
    overtime_rate,
)
from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import CalculationError, NotFoundError, PayrollCopilotError
from payroll_copilot.db.models import Employee, OvertimeEntry, PayAdjustment, PayrollRun
from payroll_copilot.db.models.adjustments import ACTIVE, CANCELLED
from payroll_copilot.domain.enums import PayrollRunStatus
from payroll_copilot.domain.salary import PayLine
from payroll_copilot.repositories.salary_repo import SalaryRepository
from payroll_copilot.rules.loader import read_yaml
from payroll_copilot.services import audit
from payroll_copilot.services.salary_config import salary_heads

ZERO = Decimal(0)
# heads a one-off adjustment may use (statutory heads are computed, never entered)
ADJUSTMENT_HEADS = {
    "EARNING": ("OTHER_EARNING", "OTHER_EARNING_NT", "BONUS"),
    "DEDUCTION": ("OTHER_DED", "LOAN"),
}
MAX_ADJUSTMENT = Decimal("10000000")


class AdjustmentError(PayrollCopilotError):
    """An overtime entry or pay adjustment that fails validation."""


@lru_cache
def _policy(path: Path) -> OvertimePolicy:
    return OvertimePolicy(**read_yaml(path)["overtime"])


def overtime_policy() -> OvertimePolicy:
    return _policy(get_settings().salary_heads_file.parent / "overtime.yaml")


def _money(value: Any, what: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise AdjustmentError(f"{what} must be a number") from None
    if not amount.is_finite():
        raise AdjustmentError(f"{what} must be a number")
    return amount


def _not_locked(session: Session, employee: Employee, period: PayrollPeriod) -> None:
    run = session.scalar(select(PayrollRun).where(PayrollRun.employee_id == employee.id,
                                                  PayrollRun.period == str(period)))
    if run is not None and run.status is PayrollRunStatus.LOCKED:
        raise AdjustmentError(f"Payroll {period.label} of {employee.employee_code} is finalized "
                              "(locked); it cannot be changed")  # fmt: skip


def _employed(employee: Employee, day: date) -> None:
    if day < employee.doj or (employee.lwd and day > employee.lwd):
        raise AdjustmentError(f"{employee.full_name} was not employed on {day}")


# ---------------------------------------------------------------------- overtime


def month_overtime(session: Session, employee: Employee,
                   period: PayrollPeriod) -> list[OvertimeEntry]:  # fmt: skip
    return list(session.scalars(select(OvertimeEntry).where(
        OvertimeEntry.employee_id == employee.id, OvertimeEntry.period == str(period),
        OvertimeEntry.status == ACTIVE).order_by(OvertimeEntry.work_date, OvertimeEntry.id)))


def quote_overtime(session: Session, employee: Employee, work_date: date, hours: Any,
                   period: PayrollPeriod | None = None) -> dict[str, Any]:  # fmt: skip
    """Rate, amount and formula for overtime on ``work_date`` - validated, not stored."""
    period = period or PayrollPeriod.from_date(work_date)
    hours = _money(hours, "Overtime hours")
    policy = overtime_policy()
    _employed(employee, work_date)
    if work_date > date.today():
        raise AdjustmentError("Overtime cannot be recorded for a future date")
    if work_date < policy.effective_from:
        raise AdjustmentError(f"The overtime policy starts on {policy.effective_from}")
    if policy.eligible_categories and employee.employee_category not in policy.eligible_categories:
        raise AdjustmentError(f"Employee category {employee.employee_category} is not eligible "
                              "for overtime under the company policy")  # fmt: skip
    so_far = sum((e.hours for e in month_overtime(session, employee, period)), ZERO)
    try:
        check_hours(policy, hours, so_far)
        assignments = SalaryRepository(session).assignments_between(
            employee.id, work_date, work_date)  # fmt: skip
        if not assignments and employee.ot_rate_per_hour is None:
            raise CalculationError(f"No salary structure on {work_date} to base overtime on")
        components = {c.head_code: c.amount for c in assignments[-1].components} if (
            assignments) else {}  # fmt: skip
        rate = overtime_rate(policy, components, employee_rate=employee.ot_rate_per_hour,
                             employee_code=employee.employee_code, period=str(period))
        amount, formula = overtime_amount(hours, rate.value)
    except CalculationError as exc:
        raise AdjustmentError(str(exc)) from exc
    return {
        "employee_code": employee.employee_code, "period": str(period),
        "work_date": work_date.isoformat(), "hours": hours, "rate_per_hour": rate.value,
        "rate_source": rate.inputs["source"], "rate_formula": rate.formula,
        "rate_steps": [{"label": s.label, "formula": s.formula, "result": s.result}
                       for s in rate.steps],
        "amount": amount, "formula": formula, "month_hours_before": so_far,
        "warnings": rate.warnings,
    }  # fmt: skip


def add_overtime(session: Session, actor: Employee, employee: Employee, work_date: date,
                 hours: Any, reason: str | None = None, period: PayrollPeriod | None = None,
                 request_id: str | None = None) -> OvertimeEntry:  # fmt: skip
    period = period or PayrollPeriod.from_date(work_date)
    _not_locked(session, employee, period)
    q = quote_overtime(session, employee, work_date, hours, period)
    entry = OvertimeEntry(
        employee_id=employee.id, period=str(period), work_date=work_date, hours=q["hours"],
        rate_per_hour=q["rate_per_hour"], rate_source=q["rate_source"][:200],
        amount=q["amount"], formula=q["formula"][:300], reason=reason, created_by_id=actor.id,
    )  # fmt: skip
    session.add(entry)
    session.flush()
    audit.record(session, "overtime.add", actor=actor, entity="overtime_entry",
                 entity_id=entry.id, new={"employee_code": employee.employee_code,
                                          "period": str(period), "work_date": work_date,
                                          "hours": entry.hours, "amount": entry.amount,
                                          "formula": entry.formula, "reason": reason},
                 request_id=request_id)  # fmt: skip
    return entry


# ---------------------------------------------------------------------- adjustments


def validate_adjustment(kind: str, head_code: str, amount: Any) -> Decimal:
    kind = kind.upper()
    if kind not in ADJUSTMENT_HEADS:
        raise AdjustmentError("Adjustment kind must be EARNING or DEDUCTION")
    if head_code not in ADJUSTMENT_HEADS[kind]:
        allowed = ", ".join(salary_heads()[h].name for h in ADJUSTMENT_HEADS[kind])
        raise AdjustmentError(f"{head_code} cannot be used for a one-off {kind.lower()}; "
                              f"use one of: {allowed}")  # fmt: skip
    value = _money(amount, "Amount")
    if not 0 < value <= MAX_ADJUSTMENT:
        raise AdjustmentError("Amount must be more than ₹0 and at most ₹1,00,00,000")
    if value != value.quantize(Decimal("0.01")):
        raise AdjustmentError("Amount can have at most 2 decimal places")
    return value


def add_adjustment(session: Session, actor: Employee, employee: Employee,
                   period: PayrollPeriod, kind: str, head_code: str, amount: Any, reason: str,
                   label: str | None = None,
                   request_id: str | None = None) -> PayAdjustment:  # fmt: skip
    value = validate_adjustment(kind, head_code, amount)
    if len((reason or "").strip()) < 5:
        raise AdjustmentError("A reason (at least 5 characters) is required")
    _employed(employee, min(max(employee.doj, period.start), period.end))
    _not_locked(session, employee, period)
    row = PayAdjustment(employee_id=employee.id, period=str(period), kind=kind.upper(),
                        head_code=head_code, label=label, amount=value, reason=reason.strip(),
                        created_by_id=actor.id)  # fmt: skip
    session.add(row)
    session.flush()
    audit.record(session, "adjustment.add", actor=actor, entity="pay_adjustment",
                 entity_id=row.id, new={"employee_code": employee.employee_code,
                                        "period": str(period), "kind": row.kind,
                                        "head_code": head_code, "amount": value,
                                        "label": label, "reason": row.reason},
                 request_id=request_id)  # fmt: skip
    return row


def month_adjustments(session: Session, employee: Employee,
                      period: PayrollPeriod) -> list[PayAdjustment]:  # fmt: skip
    return list(session.scalars(select(PayAdjustment).where(
        PayAdjustment.employee_id == employee.id, PayAdjustment.period == str(period),
        PayAdjustment.status == ACTIVE).order_by(PayAdjustment.id)))


def cancel(session: Session, actor: Employee, row: OvertimeEntry | PayAdjustment,
           reason: str) -> None:  # fmt: skip
    """Reverse an entry: kept with status CANCELLED, who and why (never deleted)."""
    if row.status != ACTIVE:
        raise AdjustmentError("Already cancelled")
    if len((reason or "").strip()) < 5:
        raise AdjustmentError("A reason (at least 5 characters) is required to cancel")
    employee = session.get(Employee, row.employee_id)
    if employee is None:
        raise NotFoundError("Employee not found")
    _not_locked(session, employee, PayrollPeriod.parse(row.period))
    row.status, row.cancelled_by_id, row.cancel_reason = CANCELLED, actor.id, reason.strip()
    row.cancelled_at = datetime.now(UTC)
    entity = "overtime_entry" if isinstance(row, OvertimeEntry) else "pay_adjustment"
    audit.record(session, f"{entity.split('_')[0]}.cancel", actor=actor, entity=entity,
                 entity_id=row.id, old={"status": ACTIVE, "amount": row.amount},
                 new={"status": CANCELLED, "reason": row.cancel_reason})  # fmt: skip
    session.flush()


# ---------------------------------------------------------------------- payroll feed


def payroll_lines(session: Session, employee: Employee,
                  period: PayrollPeriod) -> tuple[list[PayLine], list[PayLine], dict]:  # fmt: skip
    """(extra earnings, extra deductions, detail for the payslip) of the month."""
    overtime = month_overtime(session, employee, period)
    adjustments = month_adjustments(session, employee, period)
    earnings: dict[str, Decimal] = {}
    deductions: dict[str, Decimal] = {}
    if overtime:
        earnings["OVERTIME"] = sum((e.amount for e in overtime), ZERO)
    for a in adjustments:
        bucket = earnings if a.kind == "EARNING" else deductions
        bucket[a.head_code] = bucket.get(a.head_code, ZERO) + a.amount
    detail = {
        "overtime": [{"id": e.id, "work_date": e.work_date.isoformat(), "hours": str(e.hours),
                      "rate_per_hour": str(e.rate_per_hour), "rate_source": e.rate_source,
                      "amount": str(e.amount), "formula": e.formula} for e in overtime],
        "adjustments": [{"id": a.id, "kind": a.kind, "head_code": a.head_code,
                         "label": a.label, "amount": str(a.amount), "reason": a.reason}
                        for a in adjustments],
    }  # fmt: skip
    return ([PayLine(head_code=h, amount=v) for h, v in earnings.items()],
            [PayLine(head_code=h, amount=v) for h, v in deductions.items()], detail)


def overtime_overview(session: Session, employee: Employee,
                      period: PayrollPeriod) -> dict[str, Any]:  # fmt: skip
    """Policy, the employee's current rate and the month's entries / adjustments."""
    policy = overtime_policy()
    rate: dict[str, Any] | None = None
    rate_error = None
    day = min(period.end, date.today())
    try:
        assignments = SalaryRepository(session).assignments_between(employee.id, day, day)
        components = {c.head_code: c.amount for c in assignments[-1].components} if (
            assignments) else {}  # fmt: skip
        r = overtime_rate(policy, components, employee_rate=employee.ot_rate_per_hour)
        rate = {"rate_per_hour": str(r.value), "source": r.inputs["source"],
                "formula": r.formula,
                "steps": [{"label": s.label, "formula": s.formula,
                           "result": None if s.result is None else str(s.result)}
                          for s in r.steps]}  # fmt: skip
    except CalculationError as exc:
        rate_error = str(exc)
    entries = month_overtime(session, employee, period)
    _, _, detail = payroll_lines(session, employee, period)
    return {
        "policy": {"version": policy.version, "method": policy.method,
                   "enabled": policy.enabled,
                   "max_hours_per_day": str(policy.max_hours_per_day or ""),
                   "max_hours_per_month": str(policy.max_hours_per_month or "")},
        "rate": rate, "rate_error": rate_error,
        "hours": str(sum((e.hours for e in entries), ZERO)),
        "amount": str(sum((e.amount for e in entries), ZERO)),
        **detail,
    }  # fmt: skip
