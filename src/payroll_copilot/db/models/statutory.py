"""Effective-dated statutory rates, company settings and the PF calculation audit."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin, enum_column, utc_now
from payroll_copilot.db.types import Money, Rate
from payroll_copilot.domain.enums import RuleStatus


class StatutoryRate(IdMixin, TimestampMixin, Base):
    """One rate (with its own wage ceiling) valid for an inclusive date range."""

    __tablename__ = "statutory_rates"
    __table_args__ = (
        UniqueConstraint("rule_family", "rate_name", "effective_from"),
        Index("ix_statutory_rates_lookup", "rule_family", "rate_name", "effective_from"),
    )

    rule_family: Mapped[str] = mapped_column(String(16))  # PF
    rate_name: Mapped[str] = mapped_column(String(32))  # EE_PF, EPS, EDLI, ...
    rule_version: Mapped[str] = mapped_column(String(32))  # "EPF v2"
    rate_value: Mapped[Decimal] = mapped_column(Rate)
    wage_ceiling: Mapped[Decimal | None] = mapped_column(Money)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(Text)
    status: Mapped[RuleStatus] = mapped_column(enum_column(RuleStatus))
    content_hash: Mapped[str] = mapped_column(String(64))


class CompanySetting(IdMixin, Base):
    """Company configuration value, effective from a date (latest row on a date wins)."""

    __tablename__ = "company_settings"
    __table_args__ = (UniqueConstraint("key", "effective_from"),)

    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[Any] = mapped_column(JSON)
    effective_from: Mapped[date] = mapped_column(Date)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PfCalculationAudit(IdMixin, Base):
    """Every stored PF calculation: inputs, segments, rule versions, config, result."""

    __tablename__ = "pf_calculation_audit"
    __table_args__ = (Index("ix_pf_audit_employee_period", "employee_id", "period"),)

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    period: Mapped[str] = mapped_column(String(7))
    batch_id: Mapped[str | None] = mapped_column(String(32))
    doj: Mapped[date | None] = mapped_column(Date)
    pf_applicable_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))  # OK | NOT_APPLICABLE | ERROR
    rule_versions: Mapped[list[str]] = mapped_column(JSON)
    segments: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    totals: Mapped[dict[str, Any]] = mapped_column(JSON)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    lop_days: Mapped[Decimal] = mapped_column(Money)
    engine_version: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(64))
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
