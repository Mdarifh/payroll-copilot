"""Salary heads, structure templates and employee salary assignments."""

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin, enum_column
from payroll_copilot.db.types import Money, Rate
from payroll_copilot.domain.enums import AssignmentReason, CalcType, HeadCategory, HeadType

if TYPE_CHECKING:
    from payroll_copilot.db.models.employee import Employee


class SalaryHead(IdMixin, TimestampMixin, Base):
    """Catalog of every pay component. Flags decide statutory wage bases."""

    __tablename__ = "salary_heads"

    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[HeadCategory] = mapped_column(enum_column(HeadCategory))
    head_type: Mapped[HeadType] = mapped_column(enum_column(HeadType))
    is_taxable: Mapped[bool] = mapped_column(default=False)
    is_pf_wage: Mapped[bool] = mapped_column(default=False)
    is_esic_wage: Mapped[bool] = mapped_column(default=False)
    is_pt_wage: Mapped[bool] = mapped_column(default=False)
    is_lwf_wage: Mapped[bool] = mapped_column(default=False)
    is_prorated: Mapped[bool] = mapped_column(default=False)
    part_of_gross: Mapped[bool] = mapped_column(default=False)
    part_of_ctc: Mapped[bool] = mapped_column(default=False)
    display_order: Mapped[int] = mapped_column(default=0)


class SalaryStructure(IdMixin, TimestampMixin, Base):
    """Reusable template (e.g. STD-2026) describing how gross splits into heads."""

    __tablename__ = "salary_structures"
    __table_args__ = (
        UniqueConstraint("code", "version"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="valid_range"
        ),
    )

    code: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(default=1)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(500))
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)

    components: Mapped[list["SalaryStructureComponent"]] = relationship(
        back_populates="structure",
        order_by="SalaryStructureComponent.sequence",
        cascade="all, delete-orphan",
    )


class SalaryStructureComponent(IdMixin, Base):
    __tablename__ = "salary_structure_components"
    __table_args__ = (UniqueConstraint("structure_id", "head_code"),)

    structure_id: Mapped[int] = mapped_column(ForeignKey("salary_structures.id"))
    head_code: Mapped[str] = mapped_column(ForeignKey("salary_heads.code"))
    calc_type: Mapped[CalcType] = mapped_column(enum_column(CalcType))
    value: Mapped[Decimal | None] = mapped_column(Rate)  # amount for FIXED, % otherwise
    base_head_code: Mapped[str | None] = mapped_column(ForeignKey("salary_heads.code"))
    sequence: Mapped[int] = mapped_column(default=0)

    structure: Mapped[SalaryStructure] = relationship(back_populates="components")


class EmployeeSalaryStructure(IdMixin, TimestampMixin, Base):
    """An employee's structure for a date range. Increments create a new row."""

    __tablename__ = "employee_salary_structure"
    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="valid_range"
        ),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    structure_id: Mapped[int] = mapped_column(ForeignKey("salary_structures.id"))
    monthly_gross: Mapped[Decimal] = mapped_column(Money)
    annual_ctc: Mapped[Decimal | None] = mapped_column(Money)  # derived in Phase 3
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[AssignmentReason] = mapped_column(enum_column(AssignmentReason))

    employee: Mapped["Employee"] = relationship(back_populates="salary_assignments")
    structure: Mapped[SalaryStructure] = relationship()
    components: Mapped[list["EmployeeSalaryComponent"]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )


class EmployeeSalaryComponent(IdMixin, Base):
    """Full-month (un-prorated) amount of one head for one assignment."""

    __tablename__ = "employee_salary_components"
    __table_args__ = (UniqueConstraint("assignment_id", "head_code"),)

    assignment_id: Mapped[int] = mapped_column(ForeignKey("employee_salary_structure.id"))
    head_code: Mapped[str] = mapped_column(ForeignKey("salary_heads.code"))
    monthly_amount: Mapped[Decimal] = mapped_column(Money)

    assignment: Mapped[EmployeeSalaryStructure] = relationship(back_populates="components")
