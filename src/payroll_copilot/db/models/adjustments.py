"""Overtime entries and one-off pay adjustments (miscellaneous earning / deduction, bonus).

Payroll reads ACTIVE rows of the month. Rows are never deleted: a correction cancels the
row (status CANCELLED, with who and why) so the history stays complete.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin
from payroll_copilot.db.types import Days, Money

ACTIVE, CANCELLED = "ACTIVE", "CANCELLED"


class OvertimeEntry(IdMixin, TimestampMixin, Base):
    """Overtime worked on one day. Rate and amount are fixed when the entry is created,
    with the source of the rate, so a later salary change does not rewrite history."""

    __tablename__ = "overtime_entries"
    __table_args__ = (
        CheckConstraint("hours > 0", name="positive_hours"),
        Index("ix_overtime_employee_period", "employee_id", "period"),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    period: Mapped[str] = mapped_column(String(7))  # payroll month that pays it, YYYY-MM
    work_date: Mapped[date] = mapped_column(Date)
    hours: Mapped[Decimal] = mapped_column(Days)
    rate_per_hour: Mapped[Decimal] = mapped_column(Money)
    rate_source: Mapped[str] = mapped_column(String(200))  # e.g. "Overtime policy v1: 2 x ..."
    amount: Mapped[Decimal] = mapped_column(Money)
    formula: Mapped[str] = mapped_column(String(300))  # "6 h × ₹250.00/h = ₹1,500"
    reason: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(12), default=ACTIVE)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    cancelled_by_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(500))


class PayAdjustment(IdMixin, TimestampMixin, Base):
    """A one-off earning or deduction for a payroll month (head from salary config)."""

    __tablename__ = "pay_adjustments"
    __table_args__ = (
        CheckConstraint("kind IN ('EARNING', 'DEDUCTION')", name="valid_kind"),
        Index("ix_pay_adjustments_employee_period", "employee_id", "period"),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    period: Mapped[str] = mapped_column(String(7))
    kind: Mapped[str] = mapped_column(String(10))  # EARNING | DEDUCTION
    head_code: Mapped[str] = mapped_column(String(32))  # OTHER_EARNING, BONUS, OTHER_DED, ...
    label: Mapped[str | None] = mapped_column(String(100))  # e.g. "Referral bonus"
    amount: Mapped[Decimal] = mapped_column(Money)
    reason: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(12), default=ACTIVE)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    cancelled_by_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
