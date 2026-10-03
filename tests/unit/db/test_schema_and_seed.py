"""Migrations build the same schema as the models; the demo seed is complete and sane."""

from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, func, inspect, select

from payroll_copilot.auth.security import verify_password
from payroll_copilot.db.base import Base
from payroll_copilot.db.migrate import upgrade_to_head
from payroll_copilot.db.models import Attendance, Employee, Leave, SalaryStructure
from payroll_copilot.db.seed import leave_days, seed_demo_data
from payroll_copilot.domain.enums import AttendanceStatus, LeaveStatus, Role

SPEC_TABLES = {"employees", "salary_structure", "attendance", "leaves", "lop_records",
               "payroll_runs", "audit_log"}  # fmt: skip


def test_migration_matches_models(temp_db_url):
    upgrade_to_head(temp_db_url)
    engine = create_engine(temp_db_url)
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()
    assert tables == set(Base.metadata.tables)
    assert tables >= SPEC_TABLES


def _employee(session, code) -> Employee:
    return session.scalar(select(Employee).where(Employee.employee_code == code))


def test_seed_counts_and_roles(seeded_session):
    roles = dict(seeded_session.execute(select(Employee.employee_code, Employee.role)).all())
    assert len(roles) == 12
    assert [c for c, r in roles.items() if r is Role.HR] == ["HR-001", "HR-002"]
    assert [c for c, r in roles.items() if r is Role.MANAGER] == ["MGR-001"]
    assert {roles["PM-001"], roles["PX-001"], roles["VW-001"]} == {
        Role.PAYROLL_MANAGER, Role.PAYROLL_EXECUTIVE, Role.VIEWER}


def test_passwords_are_hashed(seeded_session):
    hr, emp = _employee(seeded_session, "HR-001"), _employee(seeded_session, "EMP-101")
    assert hr.password_hash.startswith("$2b$") and "Hr@12345" not in hr.password_hash
    assert verify_password("Hr@12345", hr.password_hash)
    assert verify_password("Employee@123", emp.password_hash)
    assert not verify_password("Hr@12345", emp.password_hash)


def test_salary_comes_from_the_structure_engine(seeded_session):
    emp = _employee(seeded_session, "EMP-101")
    rows = {r.head_code: r.monthly_amount for r in seeded_session.scalars(
        select(SalaryStructure).where(SalaryStructure.employee_id == emp.id))}  # fmt: skip
    assert rows == {"BASIC": Decimal(30000), "HRA": Decimal(15000), "CONVEYANCE": Decimal(1600),
                    "MEDICAL": Decimal(1250), "SPECIAL": Decimal(12150)}  # fmt: skip
    assert sum(rows.values()) == Decimal(60000)


def _september(session, code) -> dict[AttendanceStatus, int]:
    emp = _employee(session, code)
    in_sep = Attendance.day.between(date(2026, 9, 1), date(2026, 9, 30))
    rows = session.execute(
        select(Attendance.status, func.count())
        .where(Attendance.employee_id == emp.id, in_sep)
        .group_by(Attendance.status)
    ).all()
    return dict(rows)


def test_attendance_scenarios(seeded_session):
    s = AttendanceStatus
    assert _september(seeded_session, "EMP-101") == {
        s.PRESENT: 24, s.ABSENT: 1, s.WEEK_OFF: 4, s.HOLIDAY: 1}  # fmt: skip
    assert _september(seeded_session, "EMP-102")[s.ABSENT] == 2
    assert _september(seeded_session, "EMP-102")[s.LEAVE] == 2
    joiner = _september(seeded_session, "EMP-105")  # joined 15-Sep: 16 employed days
    assert sum(joiner.values()) == 16 and s.HOLIDAY not in joiner


def test_leave_rows_link_to_attendance(seeded_session):
    leave = seeded_session.scalar(select(Leave).where(Leave.from_date == date(2026, 9, 10)))
    assert (leave.days, leave.status) == (Decimal(2), LeaveStatus.APPROVED)
    linked = seeded_session.scalars(select(Attendance).where(Attendance.leave_id == leave.id))
    assert [a.day.day for a in linked] == [10, 11]
    pending = seeded_session.scalar(select(Leave).where(Leave.status == LeaveStatus.PENDING))
    assert pending.from_date == date(2026, 10, 8)


def test_leave_days_skip_sundays_and_holidays():
    holidays = {date(2026, 9, 14)}  # Sat 12, Sun 13, holiday Mon 14, Tue 15
    assert leave_days(date(2026, 9, 12), date(2026, 9, 15), holidays) == Decimal(2)


def test_seed_is_idempotent(seeded_session):
    report = seed_demo_data(seeded_session)
    assert report.skipped
    assert seeded_session.scalar(select(func.count()).select_from(Employee)) == 12
