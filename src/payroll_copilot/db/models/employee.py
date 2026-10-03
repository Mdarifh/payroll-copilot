"""Employee master data. Every employee (and HR user) logs in with email + password."""

from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin, enum_column
from payroll_copilot.db.types import Money
from payroll_copilot.domain.enums import (
    EmployeeStatus,
    Gender,
    PaidDaysBasis,
    Role,
    TaxRegime,
)


class Employee(IdMixin, TimestampMixin, Base):
    __tablename__ = "employees"
    __table_args__ = (CheckConstraint("lwd IS NULL OR lwd >= doj", name="lwd_after_doj"),)

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
    location: Mapped[str | None] = mapped_column(String(100))  # office, e.g. "Mumbai"
    employee_category: Mapped[str] = mapped_column(String(16), default="REGULAR")  # INTERN, ...
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    paid_days_basis: Mapped[PaidDaysBasis | None] = mapped_column(
        enum_column(PaidDaysBasis)
    )  # NULL => company default (DEFAULT_PAID_DAYS_BASIS)
    # overtime ₹/hour agreed for this employee; NULL => company overtime policy
    ot_rate_per_hour: Mapped[Decimal | None] = mapped_column(Money)

    # Login
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(100))  # bcrypt
    role: Mapped[Role] = mapped_column(enum_column(Role), default=Role.EMPLOYEE)
    login_enabled: Mapped[bool] = mapped_column(default=True)

    # PF
    pf_applicable: Mapped[bool] = mapped_column(default=True)
    uan: Mapped[str | None] = mapped_column(String(16))
    pf_join_date: Mapped[date | None] = mapped_column(Date)  # legacy; see pf_applicable_date
    # date PF contributions start (may differ from DOJ); required when pf_applicable
    pf_applicable_date: Mapped[date | None] = mapped_column(Date)
    pf_exempt_reason: Mapped[str | None] = mapped_column(String(200))  # why PF is not applicable
    eps_member_from: Mapped[date | None] = mapped_column(Date)  # EPS starts later (e.g. 17-09-2026)
    pf_wage_policy: Mapped[str | None] = mapped_column(String(20))  # None = company default
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
