"""Generated dummy employees: repeatable, varied, valid for the payroll engine."""

from collections import Counter
from datetime import date

from sqlalchemy import func, select

from payroll_copilot.db.dummy import dummy_employee_specs
from payroll_copilot.db.models import Attendance, Employee, PayrollRun
from payroll_copilot.db.seed import add_dummy_employees
from payroll_copilot.domain.enums import AttendanceStatus


def test_specs_are_repeatable_and_unique():
    a, b = dummy_employee_specs(50), dummy_employee_specs(50)
    assert a == b  # same people every run
    assert [s["employee_code"] for s in a] == [f"EMP-{n}" for n in range(201, 251)]
    assert len({s["email"] for s in a}) == 50


def test_specs_are_varied():
    specs = dummy_employee_specs(500)
    assert len(Counter(s["department"] for s in specs)) == 7
    assert len(Counter(s["work_state"] for s in specs)) == 9
    gross = [s["salary"]["monthly_gross"] for s in specs]
    assert min(gross) >= 15000 and max(gross) <= 200000
    assert all(s["esic_applicable"] == (s["salary"]["monthly_gross"] <= 21000) for s in specs)
    with_absences = sum(1 for s in specs if s["absences"])
    assert 0.12 * 500 < with_absences < 0.28 * 500  # about 20%
    for s in specs:  # absences are September working days, never on a leave day
        leave_days = {lv["from_date"] for lv in s["leaves"]}
        assert all(d.month == 9 and d.weekday() != 6 and d >= s["doj"] for d in s["absences"])
        assert not leave_days & set(s["absences"])


def test_add_dummy_employees_to_existing_db(seeded_session):
    report = add_dummy_employees(seeded_session, 20)
    assert report.employees == 20
    codes = seeded_session.scalars(select(Employee.employee_code).where(
        Employee.employee_code.like("EMP-2%")).order_by(Employee.employee_code)).all()
    assert codes[0] == "EMP-201" and codes[-1] == "EMP-220"
    joined_in_aug_or_before = seeded_session.scalar(select(func.count()).select_from(Employee)
        .where(Employee.employee_code.like("EMP-2%"), Employee.doj <= date(2026, 8, 31)))
    runs = seeded_session.scalar(select(func.count()).select_from(PayrollRun)
        .join(Employee, Employee.id == PayrollRun.employee_id)
        .where(Employee.employee_code.like("EMP-2%")))  # fmt: skip
    assert runs == joined_in_aug_or_before == report.payroll_runs  # August processed

    absent = seeded_session.scalar(select(func.count(func.distinct(Attendance.employee_id)))
        .join(Employee, Employee.id == Attendance.employee_id)
        .where(Employee.employee_code.like("EMP-2%"),
               Attendance.status == AttendanceStatus.ABSENT))  # fmt: skip
    assert absent >= 1  # some LOP candidates for HR

    more = add_dummy_employees(seeded_session, 5)  # numbering continues
    assert more.employees == 5
    last = select(func.max(Employee.employee_code)).where(Employee.employee_code.like("EMP-2%"))
    assert seeded_session.scalar(last) == "EMP-225"
