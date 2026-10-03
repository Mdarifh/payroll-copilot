"""Leave balance per financial year: entitlement (config) − approved leave taken."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import FinancialYear
from payroll_copilot.db.models import Employee, Leave
from payroll_copilot.domain.enums import LeaveStatus

ZERO = Decimal(0)


@dataclass(frozen=True)
class LeaveBalance:
    leave_type: str
    entitlement: Decimal
    taken: Decimal  # approved
    pending: Decimal  # applied, waiting for approval
    available: Decimal  # entitlement − taken − pending

    def as_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.__dict__.items()}


def leave_balances(session: Session, employee: Employee, fy: FinancialYear) -> list[LeaveBalance]:
    rows = session.scalars(select(Leave).where(
        Leave.employee_id == employee.id, Leave.from_date <= fy.end, Leave.to_date >= fy.start,
        Leave.status.in_([LeaveStatus.APPROVED, LeaveStatus.PENDING]),
    ))  # fmt: skip
    taken: dict[str, Decimal] = {}
    pending: dict[str, Decimal] = {}
    for leave in rows:
        bucket = taken if leave.status is LeaveStatus.APPROVED else pending
        bucket[leave.leave_type.value] = bucket.get(leave.leave_type.value, ZERO) + leave.days
    balances = []
    for leave_type, entitlement in get_settings().leave_entitlements.items():
        t, p = taken.get(leave_type, ZERO), pending.get(leave_type, ZERO)
        balances.append(LeaveBalance(leave_type, entitlement, t, p, entitlement - t - p))
    return balances
