"""Load the demo company (config/seed/chatbot_seed.yaml) into an empty database.

Salary components are calculated from the structure template by the payroll engine
(never typed in by hand), and daily attendance is generated from the employment
dates, Sundays, holidays, leaves and listed absences.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payroll_copilot.auth.security import hash_password
from payroll_copilot.calculations.structure import compute_structure_breakup
from payroll_copilot.core.config import PROJECT_ROOT
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.dummy import DUMMY_FIRST_NUMBER, DUMMY_PREFIX, dummy_employee_specs
from payroll_copilot.db.models import Attendance, Employee, Leave, SalaryStructure
from payroll_copilot.domain.enums import (
    AttendanceStatus,
    Gender,
    LeaveStatus,
    LeaveType,
    Role,
    TaxRegime,
)
from payroll_copilot.rules.loader import read_yaml
from payroll_copilot.services.salary_config import salary_heads, structure_templates

DEFAULT_SEED_FILE = PROJECT_ROOT / "config" / "seed" / "chatbot_seed.yaml"

_EMPLOYEE_FIELDS = {
    "employee_code", "full_name", "email", "gender", "dob", "doj", "lwd", "work_state",
    "department", "designation", "uan", "pan", "pf_join_date", "tax_regime", "pf_applicable",
    "esic_applicable", "esic_ip_number", "pt_applicable", "lwf_applicable", "location",
    "employee_category", "pf_applicable_date", "pf_exempt_reason", "eps_member_from",
    "pf_wage_policy",
}  # fmt: skip


@dataclass(frozen=True)
class SeedReport:
    employees: int
    salary_rows: int
    leaves: int
    attendance_days: int
    payroll_runs: int = 0
    skipped: bool = False

    def __str__(self) -> str:
        if self.skipped:
            return "Demo data already present - nothing seeded"
        return (f"Seeded {self.employees} employees, {self.salary_rows} salary rows, "
                f"{self.leaves} leaves, {self.attendance_days} attendance days, "
                f"{self.payroll_runs} processed payslips")  # fmt: skip


def _days(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def is_off_day(day: date, holidays: set[date]) -> bool:
    return day.weekday() == 6 or day in holidays  # Sunday or company holiday


def leave_days(start: date, end: date, holidays: set[date]) -> Decimal:
    """Working days in a leave range (Sundays and holidays are not counted)."""
    return Decimal(sum(1 for d in _days(start, end) if not is_off_day(d, holidays)))


@dataclass(frozen=True)
class _Calendar:
    holidays: set[date]
    holiday_names: dict[date, str]
    months: list[PayrollPeriod]

    @classmethod
    def of(cls, data: dict[str, Any]) -> "_Calendar":
        return cls(
            holidays={h["day"] for h in data["holidays"]},
            holiday_names={h["day"]: h["name"] for h in data["holidays"]},
            months=[PayrollPeriod.parse(m) for m in data["attendance_months"]],
        )


def seed_demo_data(
    session: Session, path: Path = DEFAULT_SEED_FILE, dummy_employees: int | None = None,
) -> SeedReport:  # fmt: skip
    """Named demo people + generated ones (``dummy_employees``, default from the seed file)."""
    if session.scalar(select(func.count()).select_from(Employee)):
        return SeedReport(0, 0, 0, 0, skipped=True)
    data: dict[str, Any] = read_yaml(path)
    count = data.get("dummy_employees", 0) if dummy_employees is None else dummy_employees
    items = [*data["employees"], *dummy_employee_specs(count)]
    hashes = {role: hash_password(pw) for role, pw in data["passwords"].items()}
    return _seed_items(session, data, items, hashes)


def add_dummy_employees(
    session: Session, count: int, path: Path = DEFAULT_SEED_FILE
) -> SeedReport:
    """Add ``count`` more generated employees to an existing database (codes continue)."""
    data: dict[str, Any] = read_yaml(path)
    codes = session.scalars(select(Employee.employee_code))
    numbers = [int(c.split("-")[1]) for c in codes
               if c.startswith(f"{DUMMY_PREFIX}-") and c.split("-")[1].isdigit()]  # fmt: skip
    start = max([DUMMY_FIRST_NUMBER - 1, *numbers]) + 1
    hashes = {"employee": hash_password(data["passwords"]["employee"])}
    return _seed_items(session, data, dummy_employee_specs(count, first_number=start), hashes)


def apply_org(session: Session, data: dict[str, Any]) -> int:
    """Fill office location (from work state) and reporting manager (per department)
    where they are still empty. Returns how many employees changed."""
    locations = data.get("locations", {})
    managers = {dept: session.scalar(select(Employee.id).where(Employee.employee_code == code))
                for dept, code in data.get("department_managers", {}).items()}  # fmt: skip
    changed = 0
    for employee in session.scalars(select(Employee)):
        before = (employee.location, employee.manager_id)
        if employee.location is None:
            employee.location = locations.get(employee.work_state)
        manager_id = managers.get(employee.department)
        if (employee.manager_id is None and manager_id and manager_id != employee.id
                and employee.role is not Role.HR):  # fmt: skip
            employee.manager_id = manager_id
        changed += before != (employee.location, employee.manager_id)
    session.flush()
    return changed


def upgrade_demo(session: Session, path: Path = DEFAULT_SEED_FILE) -> str:
    """Bring an existing demo database up to the current seed without a reset: add named
    people that are missing (e.g. HR-002, MGR-001, INT-001) and fill locations/managers."""
    data: dict[str, Any] = read_yaml(path)
    existing = set(session.scalars(select(Employee.employee_code)))
    missing = [e for e in data["employees"] if e["employee_code"] not in existing]
    report = None
    if missing:
        hashes = {role: hash_password(pw) for role, pw in data["passwords"].items()}
        report = _seed_items(session, data, missing, hashes)
    changed = apply_org(session, data)
    added = ", ".join(e["employee_code"] for e in missing) or "none"
    return f"Added: {added}. Location/manager filled for {changed} employee(s)." + (
        f" {report}" if report else "")


def _seed_items(
    session: Session, data: dict[str, Any], items: list[dict[str, Any]], hashes: dict[str, str]
) -> SeedReport:
    cal = _Calendar.of(data)
    employees, salary_rows, leave_count, attendance_days = [], 0, 0, 0
    for item in items:
        employee, *counts = _add_employee(session, item, hashes[item.get("role", "employee")],
                                          cal)  # fmt: skip
        employees.append(employee)
        salary_rows += counts[0]
        leave_count += counts[1]
        attendance_days += counts[2]
    session.flush()
    apply_org(session, data)

    from payroll_copilot.services.payslip import process_payroll  # needs rules loaded

    hr = session.scalar(select(Employee).where(Employee.role == Role.HR).order_by(Employee.id))
    runs = 0
    for month in (PayrollPeriod.parse(m) for m in data.get("processed_months", [])):
        for employee in employees:
            if employee.doj <= month.end and (employee.lwd is None or employee.lwd >= month.start):
                process_payroll(session, employee, month, run_by=hr)
                runs += 1
    session.flush()
    return SeedReport(len(items), salary_rows, leave_count, attendance_days, runs)


def _add_employee(
    session: Session, item: dict[str, Any], password_hash: str, cal: _Calendar
) -> tuple[Employee, int, int, int]:
    """One employee with salary rows, leaves and daily attendance (+ row counts)."""
    heads, templates = salary_heads(), structure_templates()
    holidays, holiday_names = cal.holidays, cal.holiday_names
    role = Role(item.get("role", "employee"))
    fields = {k: v for k, v in item.items() if k in _EMPLOYEE_FIELDS}
    fields["gender"] = Gender(fields["gender"])  # real enums: the engine reads .value
    fields["tax_regime"] = TaxRegime(fields.get("tax_regime", TaxRegime.NEW))
    if fields.get("pf_applicable", True) and not fields.get("pf_applicable_date"):
        fields["pf_applicable_date"] = fields.get("pf_join_date") or fields["doj"]
    employee = Employee(**fields, role=role, password_hash=password_hash)
    session.add(employee)
    session.flush()
    salary_rows = leave_count = attendance_days = 0

    salary = item["salary"]
    breakup = compute_structure_breakup(
        templates[salary["template"]], salary["monthly_gross"], heads,
        employee_code=employee.employee_code,
    )  # fmt: skip
    for head_code, amount in breakup.breakdown.items():
        session.add(SalaryStructure(
            employee_id=employee.id, effective_from=salary["effective_from"],
            head_code=head_code, monthly_amount=amount, template_code=salary["template"],
        ))  # fmt: skip
        salary_rows += 1

    leave_by_day: dict[date, Leave] = {}
    for spec in item.get("leaves", []):
        status = LeaveStatus(spec["status"])
        leave = Leave(
            employee_id=employee.id, leave_type=LeaveType(spec["leave_type"]),
            from_date=spec["from_date"], to_date=spec["to_date"],
            days=leave_days(spec["from_date"], spec["to_date"], holidays),
            reason=spec.get("reason"), status=status,
        )  # fmt: skip
        session.add(leave)
        session.flush()
        leave_count += 1
        if status is LeaveStatus.APPROVED:
            for day in _days(leave.from_date, leave.to_date):
                leave_by_day[day] = leave

    absences = set(item.get("absences", []))
    for month in cal.months:
        start = max(month.start, employee.doj)
        end = min(month.end, employee.lwd or month.end)
        for day in _days(start, end):
            leave = leave_by_day.get(day)
            if day.weekday() == 6:
                status, remarks = AttendanceStatus.WEEK_OFF, None
            elif day in holidays:
                status, remarks = AttendanceStatus.HOLIDAY, holiday_names[day]
            elif leave is not None:
                status, remarks = AttendanceStatus.LEAVE, f"{leave.leave_type.value} leave"
            elif day in absences:
                status, remarks = AttendanceStatus.ABSENT, "No leave applied"
            else:
                status, remarks = AttendanceStatus.PRESENT, None
            session.add(Attendance(
                employee_id=employee.id, day=day, status=status, remarks=remarks,
                leave_id=leave.id if leave and status is AttendanceStatus.LEAVE else None,
            ))  # fmt: skip
            attendance_days += 1
    return employee, salary_rows, leave_count, attendance_days
