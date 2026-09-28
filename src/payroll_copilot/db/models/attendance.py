"""Monthly attendance summary."""

from decimal import Decimal

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin
from payroll_copilot.db.types import Days


class Attendance(IdMixin, TimestampMixin, Base):
    """One row per employee per payroll month.

    ``calendar_days`` is the length of the month. Every other count covers only
    the days the employee was employed in that month (DOJ..LWD), so for a
    mid-month joiner week_offs/working_days are for the employed span only.
    ``lop_reversal_days`` returns LOP wrongly deducted in an earlier month.
    """

    __tablename__ = "attendance"
    __table_args__ = (UniqueConstraint("employee_id", "period"),)

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    calendar_days: Mapped[int]
    working_days: Mapped[Decimal] = mapped_column(Days)
    week_offs: Mapped[Decimal] = mapped_column(Days)
    holidays: Mapped[Decimal] = mapped_column(Days)
    present_days: Mapped[Decimal] = mapped_column(Days)
    paid_leave_days: Mapped[Decimal] = mapped_column(Days, default=Decimal("0"))
    absent_days: Mapped[Decimal] = mapped_column(Days, default=Decimal("0"))
    lop_days: Mapped[Decimal] = mapped_column(Days, default=Decimal("0"))
    lop_reversal_days: Mapped[Decimal] = mapped_column(Days, default=Decimal("0"))
    source: Mapped[str] = mapped_column(String(32), default="SEED")
