"""Attendance domain object (decoupled from the ORM)."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class AttendanceRecord(BaseModel):
    """Monthly attendance summary.

    ``calendar_days`` is the month length; the other counts cover only the days
    the employee was employed in the month (DOJ..LWD).
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    period: str
    calendar_days: int
    working_days: Decimal
    week_offs: Decimal
    holidays: Decimal
    present_days: Decimal
    paid_leave_days: Decimal = Decimal(0)
    absent_days: Decimal = Decimal(0)
    lop_days: Decimal = Decimal(0)
    lop_reversal_days: Decimal = Decimal(0)
