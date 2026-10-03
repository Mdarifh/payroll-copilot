"""Salary structure, attendance, leaves, LOP and payroll runs."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin, enum_column
from payroll_copilot.db.types import Days, Money
from payroll_copilot.domain.enums import (
    AttendanceStatus,
    LeaveStatus,
    LeaveType,
    PayrollRunStatus,
)


class SalaryStructure(IdMixin, TimestampMixin, Base):
    """Full-month amount of one salary head for an employee, valid for a date range.

    All rows of an employee with the same ``effective_from`` form one structure
    version; monthly gross = Σ amounts of gross heads (see config/payroll).
    """

    __tablename__ = "salary_structure"
    __table_args__ = (
        UniqueConstraint("employee_id", "effective_from", "head_code"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from",
                        name="valid_range"),
    )  # fmt: skip

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    head_code: Mapped[str] = mapped_column(String(32))  # BASIC, HRA, SPECIAL, ...
    monthly_amount: Mapped[Decimal] = mapped_column(Money)
    template_code: Mapped[str | None] = mapped_column(String(32))  # e.g. STD-2026


class Attendance(IdMixin, TimestampMixin, Base):
    """One row per employee per day."""

    __tablename__ = "attendance"
    __table_args__ = (UniqueConstraint("employee_id", "day"),)

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    day: Mapped[date] = mapped_column(Date)
    status: Mapped[AttendanceStatus] = mapped_column(enum_column(AttendanceStatus))
    leave_id: Mapped[int | None] = mapped_column(ForeignKey("leaves.id"))
    remarks: Mapped[str | None] = mapped_column(String(200))


class Leave(IdMixin, TimestampMixin, Base):
    __tablename__ = "leaves"
    __table_args__ = (
        CheckConstraint("to_date >= from_date", name="valid_range"),
        CheckConstraint("days > 0", name="positive_days"),
        Index("ix_leaves_employee_dates", "employee_id", "from_date"),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    leave_type: Mapped[LeaveType] = mapped_column(enum_column(LeaveType))
    from_date: Mapped[date] = mapped_column(Date)
    to_date: Mapped[date] = mapped_column(Date)
    days: Mapped[Decimal] = mapped_column(Days)
    reason: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[LeaveStatus] = mapped_column(
        enum_column(LeaveStatus), default=LeaveStatus.PENDING
    )
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LopRecord(IdMixin, TimestampMixin, Base):
    """LOP days HR decided for an employee and payroll month (one row, updated in place;
    the audit log keeps every old value)."""

    __tablename__ = "lop_records"
    __table_args__ = (
        UniqueConstraint("employee_id", "period"),
        CheckConstraint("days >= 0", name="non_negative_days"),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    days: Mapped[Decimal] = mapped_column(Days)
    # [{"date": "2026-09-15", "days": "1"}, ...]: which days are LOP (PF is split by date)
    lop_dates: Mapped[list[dict[str, str]] | None] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(500))
    updated_by_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))


class PayrollRun(IdMixin, TimestampMixin, Base):
    """Processed payroll of one employee for one month, with the full payslip."""

    __tablename__ = "payroll_runs"
    __table_args__ = (UniqueConstraint("employee_id", "period"),)

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    status: Mapped[PayrollRunStatus] = mapped_column(
        enum_column(PayrollRunStatus), default=PayrollRunStatus.PROCESSED
    )
    version: Mapped[int] = mapped_column(default=1)  # +1 on every re-run
    paid_days: Mapped[Decimal] = mapped_column(Days)
    lop_days: Mapped[Decimal] = mapped_column(Days)
    gross_earnings: Mapped[Decimal] = mapped_column(Money)
    total_deductions: Mapped[Decimal] = mapped_column(Money)
    net_pay: Mapped[Decimal] = mapped_column(Money)
    payslip: Mapped[dict[str, Any]] = mapped_column(JSON)  # lines, traces, rule versions
    run_by_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
