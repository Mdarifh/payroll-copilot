"""Arrears, full & final settlements and tax declarations."""

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin, enum_column
from payroll_copilot.db.types import Days, Money
from payroll_copilot.domain.enums import ArrearStatus, FnFStatus, TaxRegime


class Arrear(IdMixin, TimestampMixin, Base):
    """Back pay for one head, e.g. a retrospective increment.

    ``amount`` is NULL when the arrear engine should compute it from the
    old/new monthly amounts and attendance of the arrear months.
    """

    __tablename__ = "arrears"

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    paid_in_period: Mapped[str] = mapped_column(String(7))
    from_period: Mapped[str] = mapped_column(String(7))
    to_period: Mapped[str] = mapped_column(String(7))
    head_code: Mapped[str] = mapped_column(ForeignKey("salary_heads.code"))
    old_monthly_amount: Mapped[Decimal] = mapped_column(Money)
    new_monthly_amount: Mapped[Decimal] = mapped_column(Money)
    arrear_days: Mapped[Decimal | None] = mapped_column(Days)
    amount: Mapped[Decimal | None] = mapped_column(Money)
    upload_batch: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ArrearStatus] = mapped_column(
        enum_column(ArrearStatus), default=ArrearStatus.PENDING
    )


class FnFSettlement(IdMixin, TimestampMixin, Base):
    __tablename__ = "fnf_settlements"

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), unique=True)
    lwd: Mapped[date] = mapped_column(Date)
    settlement_period: Mapped[str] = mapped_column(String(7))
    leave_encashment_days: Mapped[Decimal] = mapped_column(Days, default=Decimal("0"))
    notice_period_days: Mapped[int] = mapped_column(default=0)
    notice_days_served: Mapped[int] = mapped_column(default=0)
    other_recovery: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    status: Mapped[FnFStatus] = mapped_column(enum_column(FnFStatus), default=FnFStatus.PENDING)


class TDSOverride(IdMixin, TimestampMixin, Base):
    """An approved monthly TDS override (created only by an executed approval)."""

    __tablename__ = "tds_overrides"

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    period: Mapped[str] = mapped_column(String(7))
    amount: Mapped[Decimal] = mapped_column(Money)
    reason: Mapped[str] = mapped_column(String(500))
    approval_id: Mapped[int] = mapped_column(ForeignKey("approvals.id"))
    active: Mapped[bool] = mapped_column(default=True)


class TDSDeclaration(IdMixin, TimestampMixin, Base):
    """Employee investment / exemption declaration for a financial year.

    ``section_code`` is a free label from the TDS rule config (section numbers
    differ between the Income-tax Act 1961 and the Income-tax Act 2025).
    """

    __tablename__ = "tds_declarations"
    __table_args__ = (UniqueConstraint("employee_id", "financial_year", "section_code"),)

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    financial_year: Mapped[str] = mapped_column(String(7))
    regime: Mapped[TaxRegime] = mapped_column(enum_column(TaxRegime))
    section_code: Mapped[str] = mapped_column(String(32))
    declared_amount: Mapped[Decimal] = mapped_column(Money)
    proof_amount: Mapped[Decimal | None] = mapped_column(Money)
