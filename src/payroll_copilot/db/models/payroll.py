"""Actual payroll (from the payroll system) and our expected calculation snapshots.

Employee deductions and employer contributions live in separate tables so the
two can never be mixed up when computing net pay or CTC.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin, enum_column
from payroll_copilot.db.types import Days, Money
from payroll_copilot.domain.enums import PayrollRunStatus, PayrollSource, RuleType


class PayrollRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "payroll_runs"
    __table_args__ = (UniqueConstraint("period", "source"),)

    period: Mapped[str] = mapped_column(String(7))
    source: Mapped[PayrollSource] = mapped_column(enum_column(PayrollSource))
    status: Mapped[PayrollRunStatus] = mapped_column(
        enum_column(PayrollRunStatus), default=PayrollRunStatus.DRAFT
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    payslips: Mapped[list["Payroll"]] = relationship(back_populates="run")


class Payroll(IdMixin, TimestampMixin, Base):
    """Payslip header for one employee in one run."""

    __tablename__ = "payroll"
    __table_args__ = (UniqueConstraint("run_id", "employee_id"),)

    run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id"))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    period: Mapped[str] = mapped_column(String(7))
    paid_days: Mapped[Decimal] = mapped_column(Days)
    lop_days: Mapped[Decimal] = mapped_column(Days)
    gross_earnings: Mapped[Decimal] = mapped_column(Money)
    total_deductions: Mapped[Decimal] = mapped_column(Money)
    net_pay: Mapped[Decimal] = mapped_column(Money)
    total_employer_contribution: Mapped[Decimal] = mapped_column(Money)
    monthly_ctc: Mapped[Decimal | None] = mapped_column(Money)

    run: Mapped[PayrollRun] = relationship(back_populates="payslips")
    earnings: Mapped[list["PayrollEarning"]] = relationship(cascade="all, delete-orphan")
    deductions: Mapped[list["PayrollDeduction"]] = relationship(cascade="all, delete-orphan")
    employer_contributions: Mapped[list["EmployerContribution"]] = relationship(
        cascade="all, delete-orphan"
    )
    components: Mapped[list["PayrollComponent"]] = relationship(cascade="all, delete-orphan")


class _PayrollLine(IdMixin):
    payroll_id: Mapped[int] = mapped_column(ForeignKey("payroll.id"))
    head_code: Mapped[str] = mapped_column(ForeignKey("salary_heads.code"))
    amount: Mapped[Decimal] = mapped_column(Money)
    is_arrear: Mapped[bool] = mapped_column(default=False)


class PayrollEarning(_PayrollLine, Base):
    __tablename__ = "payroll_earnings"

    full_month_amount: Mapped[Decimal | None] = mapped_column(Money)


class PayrollDeduction(_PayrollLine, Base):
    __tablename__ = "payroll_deductions"


class EmployerContribution(_PayrollLine, Base):
    __tablename__ = "employer_contributions"


class PayrollComponent(IdMixin, TimestampMixin, Base):
    """Our EXPECTED value for one component, with the full calculation trace."""

    __tablename__ = "payroll_components"
    __table_args__ = (UniqueConstraint("payroll_id", "component"),)

    payroll_id: Mapped[int] = mapped_column(ForeignKey("payroll.id"))
    component: Mapped[str] = mapped_column(String(32))
    expected_value: Mapped[Decimal] = mapped_column(Money)
    rule_type: Mapped[RuleType | None] = mapped_column(enum_column(RuleType))
    rule_code: Mapped[str | None] = mapped_column(String(32))
    rule_version: Mapped[int | None]
    trace: Mapped[dict] = mapped_column(JSON, default=dict)
