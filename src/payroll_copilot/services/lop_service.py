"""Set one employee's LOP days for a month (validated, audited).

Same record and audit trail as the bulk LOP review (batch_service.request_run). LOP is
placed on the absences without approved leave, earliest first (a half day takes 0.5),
because PF is split by date.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Employee, LopRecord
from payroll_copilot.repositories.attendance_repo import AttendanceRepository
from payroll_copilot.services import audit
from payroll_copilot.services.pay_adjustments import AdjustmentError, _not_locked

HALF = Decimal("0.5")
ZERO = Decimal(0)


def _dates(exceptions, days: Decimal) -> list[dict[str, str]] | None:
    """LOP dates on the absences; None when there are fewer absences than LOP days."""
    if days > sum((e.days for e in exceptions), ZERO):
        return None
    result, remaining = [], days
    for e in exceptions:
        if remaining <= 0:
            break
        take = min(e.days, remaining)
        result.append({"date": e.day.isoformat(), "days": str(take)})
        remaining -= take
    return result


def set_lop(session: Session, actor: Employee, employee: Employee, period: PayrollPeriod,
            days: Decimal, reason: str, request_id: str | None = None) -> LopRecord:  # fmt: skip
    if len((reason or "").strip()) < 5:
        raise AdjustmentError("A reason (at least 5 characters) is required")
    if days < 0 or days % HALF:
        raise AdjustmentError("LOP days must be 0 or more, in steps of 0.5")
    _not_locked(session, employee, period)
    repo = AttendanceRepository(session)
    record_view = repo.get(employee.id, period)
    if record_view is None:
        raise AdjustmentError(f"No attendance for {employee.employee_code} in {period.label}")
    payable = record_view.calendar_days
    if days > payable:
        raise AdjustmentError(f"LOP cannot be more than {payable} days in {period.label}")

    record = session.scalar(select(LopRecord).where(LopRecord.employee_id == employee.id,
                                                    LopRecord.period == str(period)))
    old = None if record is None else {"lop_days": record.days, "reason": record.reason,
                                       "lop_dates": record.lop_dates}  # fmt: skip
    dates = _dates(repo.exceptions(employee.id, period), days)
    if record is None:
        record = LopRecord(employee_id=employee.id, period=str(period), days=days,
                           lop_dates=dates, reason=reason.strip(), updated_by_id=actor.id)
        session.add(record)
    else:
        record.days, record.lop_dates = days, dates
        record.reason, record.updated_by_id = reason.strip(), actor.id
    session.flush()
    audit.record(session, "lop.update", actor=actor, entity="lop_record", entity_id=record.id,
                 old=old, new={"employee_code": employee.employee_code, "period": str(period),
                               "lop_days": days, "lop_dates": dates, "reason": record.reason},
                 request_id=request_id)  # fmt: skip
    return record
