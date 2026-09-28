"""Company and employee master data."""

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin, enum_column
from payroll_copilot.domain.enums import (
    EmployeeStatus,
    Gender,
    PaidDaysBasis,
    TaxRegime,
)

if TYPE_CHECKING:
    from payroll_copilot.db.models.salary import EmployeeSalaryStructure


class Company(IdMixin, TimestampMixin, Base):
    __tablename__ = "companies"

    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    state_code: Mapped[str] = mapped_column(String(4))
    pf_establishment_code: Mapped[str | None] = mapped_column(String(32))
    esic_employer_code: Mapped[str | None] = mapped_column(String(32))
    paid_days_basis: Mapped[PaidDaysBasis] = mapped_column(
        enum_column(PaidDaysBasis), default=PaidDaysBasis.CALENDAR
    )

    employees: Mapped[list["Employee"]] = relationship(back_populates="company")


class Employee(IdMixin, TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (CheckConstraint("lwd IS NULL OR lwd >= doj", name="lwd_after_doj"),)

    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    employee_code: Mapped[str] = mapped_column(String(32), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    gender: Mapped[Gender] = mapped_column(enum_column(Gender))
    dob: Mapped[date] = mapped_column(Date)
    doj: Mapped[date] = mapped_column(Date)
    lwd: Mapped[date | None] = mapped_column(Date)
    status: Mapped[EmployeeStatus] = mapped_column(
        enum_column(EmployeeStatus), default=EmployeeStatus.ACTIVE
    )
    work_state: Mapped[str] = mapped_column(String(4))  # drives PT / LWF rules
    department: Mapped[str | None] = mapped_column(String(100))
    designation: Mapped[str | None] = mapped_column(String(100))
    pay_group: Mapped[str | None] = mapped_column(String(32))
    paid_days_basis: Mapped[PaidDaysBasis | None] = mapped_column(
        enum_column(PaidDaysBasis)
    )  # NULL => company default

    # PF
    pf_applicable: Mapped[bool] = mapped_column(default=True)
    uan: Mapped[str | None] = mapped_column(String(16))
    pf_join_date: Mapped[date | None] = mapped_column(Date)
    eps_eligible: Mapped[bool] = mapped_column(default=True)
    # True: contribute on min(wage, ceiling). False: contribute on actual PF wage.
    pf_wage_restricted: Mapped[bool] = mapped_column(default=True)

    # ESIC / PT / LWF
    esic_applicable: Mapped[bool] = mapped_column(default=False)
    esic_ip_number: Mapped[str | None] = mapped_column(String(20))
    pt_applicable: Mapped[bool] = mapped_column(default=True)
    lwf_applicable: Mapped[bool] = mapped_column(default=True)
    is_disabled: Mapped[bool] = mapped_column(default=False)

    # Tax
    tax_regime: Mapped[TaxRegime] = mapped_column(enum_column(TaxRegime), default=TaxRegime.NEW)
    pan: Mapped[str | None] = mapped_column(String(10))

    company: Mapped[Company] = relationship(back_populates="employees")
    salary_assignments: Mapped[list["EmployeeSalaryStructure"]] = relationship(
        back_populates="employee", order_by="EmployeeSalaryStructure.effective_from"
    )
