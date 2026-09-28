"""Versioned statutory rule tables.

Every rule row is one *version* of a rule family (``rule_code``), valid for an
inclusive date range and optionally restricted by ``applicability`` conditions
(evaluated by ``payroll_copilot.rules.applicability``). Rule values come from
``config/rules/*.yaml`` via the rule loader; they are never hard-coded in the
calculation engine.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, Date, ForeignKey, String, UniqueConstraint
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship

from payroll_copilot.core.money import RoundingMode
from payroll_copilot.db.base import Base, IdMixin, TimestampMixin, enum_column
from payroll_copilot.db.types import Money, Rate
from payroll_copilot.domain.enums import (
    AgeCategory,
    CeilingProration,
    Frequency,
    Gender,
    RuleStatus,
    TaxRegime,
    WageBasis,
)


class RuleVersionMixin(IdMixin, TimestampMixin):
    rule_code: Mapped[str] = mapped_column(String(32))
    version: Mapped[int]
    name: Mapped[str] = mapped_column(String(200))
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)  # NULL => open-ended
    status: Mapped[RuleStatus] = mapped_column(
        enum_column(RuleStatus), default=RuleStatus.UNVERIFIED
    )
    applicability: Mapped[list] = mapped_column(JSON, default=list)
    source_ref: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(String(2000))
    content_hash: Mapped[str] = mapped_column(String(64))
    rounding_mode: Mapped[RoundingMode] = mapped_column(
        enum_column(RoundingMode), default=RoundingMode.HALF_UP
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        return (
            UniqueConstraint("rule_code", "version"),
            CheckConstraint(
                "effective_to IS NULL OR effective_to >= effective_from", name="valid_range"
            ),
        )


class PFRule(RuleVersionMixin, Base):
    __tablename__ = "pf_rules"

    employee_rate: Mapped[Decimal] = mapped_column(Rate)
    employer_rate: Mapped[Decimal] = mapped_column(Rate)  # EPF + EPS together
    eps_rate: Mapped[Decimal] = mapped_column(Rate)
    edli_rate: Mapped[Decimal] = mapped_column(Rate)
    admin_rate: Mapped[Decimal] = mapped_column(Rate)
    admin_min_amount: Mapped[Decimal | None] = mapped_column(Money)  # per establishment
    pf_wage_ceiling: Mapped[Decimal] = mapped_column(Money)
    eps_wage_ceiling: Mapped[Decimal] = mapped_column(Money)
    edli_wage_ceiling: Mapped[Decimal] = mapped_column(Money)
    eps_max_age: Mapped[int | None]
    ceiling_proration: Mapped[CeilingProration] = mapped_column(
        enum_column(CeilingProration), default=CeilingProration.NONE
    )


class ESICRule(RuleVersionMixin, Base):
    __tablename__ = "esic_rules"

    employee_rate: Mapped[Decimal] = mapped_column(Rate)
    employer_rate: Mapped[Decimal] = mapped_column(Rate)
    wage_threshold: Mapped[Decimal] = mapped_column(Money)
    disabled_wage_threshold: Mapped[Decimal | None] = mapped_column(Money)
    contribution_period_start_months: Mapped[list] = mapped_column(JSON, default=list)


class PTRule(RuleVersionMixin, Base):
    __tablename__ = "pt_rules"

    state_code: Mapped[str] = mapped_column(String(4), index=True)
    state_name: Mapped[str] = mapped_column(String(100))
    is_levied: Mapped[bool] = mapped_column(default=True)
    frequency: Mapped[Frequency] = mapped_column(enum_column(Frequency))
    wage_basis: Mapped[WageBasis] = mapped_column(enum_column(WageBasis))
    deduction_months: Mapped[list] = mapped_column(JSON, default=list)

    slabs: Mapped[list["PTSlab"]] = relationship(
        cascade="all, delete-orphan", order_by="PTSlab.sequence"
    )


class PTSlab(IdMixin, Base):
    """Wage band ``lower_exclusive < wage <= upper_inclusive`` (NULL = unbounded)."""

    __tablename__ = "pt_slabs"

    rule_id: Mapped[int] = mapped_column(ForeignKey("pt_rules.id"))
    sequence: Mapped[int]
    gender: Mapped[Gender | None] = mapped_column(enum_column(Gender))  # NULL = any
    lower_exclusive: Mapped[Decimal | None] = mapped_column(Money)
    upper_inclusive: Mapped[Decimal | None] = mapped_column(Money)
    amount: Mapped[Decimal] = mapped_column(Money)
    month_overrides: Mapped[dict] = mapped_column(JSON, default=dict)  # {"2": "300"}


class LWFRule(RuleVersionMixin, Base):
    __tablename__ = "lwf_rules"

    state_code: Mapped[str] = mapped_column(String(4), index=True)
    state_name: Mapped[str] = mapped_column(String(100))
    frequency: Mapped[Frequency] = mapped_column(enum_column(Frequency))
    deduction_months: Mapped[list] = mapped_column(JSON, default=list)
    employee_amount: Mapped[Decimal | None] = mapped_column(Money)
    employer_amount: Mapped[Decimal | None] = mapped_column(Money)
    employee_rate: Mapped[Decimal | None] = mapped_column(Rate)  # % of LWF wage
    employee_max_amount: Mapped[Decimal | None] = mapped_column(Money)
    employer_multiplier: Mapped[Decimal | None] = mapped_column(Rate)  # ER = EE × multiplier
    wage_upper_limit: Mapped[Decimal | None] = mapped_column(Money)


class TDSRule(RuleVersionMixin, Base):
    __tablename__ = "tds_rules"

    financial_year: Mapped[str] = mapped_column(String(7), index=True)
    regime: Mapped[TaxRegime] = mapped_column(enum_column(TaxRegime))
    age_category: Mapped[AgeCategory | None] = mapped_column(
        enum_column(AgeCategory)
    )  # NULL => all ages
    standard_deduction: Mapped[Decimal] = mapped_column(Money)
    rebate_income_limit: Mapped[Decimal | None] = mapped_column(Money)
    rebate_max: Mapped[Decimal | None] = mapped_column(Money)
    rebate_marginal_relief: Mapped[bool] = mapped_column(default=False)
    cess_rate: Mapped[Decimal] = mapped_column(Rate)
    surcharge: Mapped[list] = mapped_column(JSON, default=list)  # [{"above": .., "rate": ..}]
    # Section label -> maximum deductible amount; empty => no declarations allowed.
    deduction_limits: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    allow_professional_tax_deduction: Mapped[bool] = mapped_column(
        default=False, server_default=sa_false()
    )

    slabs: Mapped[list["TDSSlab"]] = relationship(
        cascade="all, delete-orphan", order_by="TDSSlab.sequence"
    )


class TDSSlab(IdMixin, Base):
    __tablename__ = "tds_slabs"

    rule_id: Mapped[int] = mapped_column(ForeignKey("tds_rules.id"))
    sequence: Mapped[int]
    lower_exclusive: Mapped[Decimal | None] = mapped_column(Money)
    upper_inclusive: Mapped[Decimal | None] = mapped_column(Money)
    rate: Mapped[Decimal] = mapped_column(Rate)
