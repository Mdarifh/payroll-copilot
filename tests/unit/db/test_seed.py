import calendar
from datetime import date, timedelta
from decimal import Decimal

import pytest
import yaml
from sqlalchemy import func, select

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import (
    Arrear,
    Attendance,
    Employee,
    EmployeeSalaryStructure,
    FnFSettlement,
    SalaryHead,
)
from payroll_copilot.db.seed import DEFAULT_SEED_FILE, SeedDataError, seed_demo_data
from payroll_copilot.domain.enums import CalcType, HeadCategory
from payroll_copilot.rules.loader import read_yaml


def _employee(session, code) -> Employee:
    return session.scalar(select(Employee).where(Employee.employee_code == code))


def test_counts(seeded_session):
    assert seeded_session.scalar(select(func.count()).select_from(Employee)) == 10
    categories = {h.category for h in seeded_session.scalars(select(SalaryHead))}
    assert categories == set(HeadCategory)


def test_seed_is_idempotent(seeded_session):
    assert seed_demo_data(seeded_session).skipped
    assert seeded_session.scalar(select(func.count()).select_from(Employee)) == 10


def test_required_scenarios_present(seeded_session):
    employees = {e.employee_code: e for e in seeded_session.scalars(select(Employee))}
    sep = PayrollPeriod(2026, 9)
    # Money/day columns are text on SQLite: compare in Python, not SQL.
    rows = seeded_session.scalars(select(Attendance).where(Attendance.period == str(sep)))
    assert any(r.lop_days > 0 for r in rows), "employee with LOP"
    assert seeded_session.scalar(select(func.count()).select_from(Arrear)), "employee with arrear"
    assert any(sep.contains(e.doj) for e in employees.values()), "new joiner"
    assert any(e.lwd and sep.contains(e.lwd) for e in employees.values()), "separated employee"
    assert seeded_session.scalar(select(func.count()).select_from(FnFSettlement)) == 1
    assert {e.pf_applicable for e in employees.values()} == {True, False}, "with/without PF"
    assert {e.esic_applicable for e in employees.values()} == {True, False}, "with/without ESIC"


def test_components_follow_structure_template(seeded_session):
    for assignment in seeded_session.scalars(select(EmployeeSalaryStructure)):
        amounts = {c.head_code: c.monthly_amount for c in assignment.components}
        assert sum(amounts.values()) == assignment.monthly_gross
        for comp in assignment.structure.components:
            if comp.calc_type is CalcType.PERCENT_OF_GROSS:
                expected = assignment.monthly_gross * comp.value / 100
            elif comp.calc_type is CalcType.PERCENT_OF_HEAD:
                expected = amounts[comp.base_head_code] * comp.value / 100
            elif comp.calc_type is CalcType.FIXED:
                expected = comp.value
            else:
                continue
            assert amounts[comp.head_code] == expected, (assignment.employee_id, comp.head_code)


def test_increment_history_has_no_gaps(seeded_session):
    meena = _employee(seeded_session, "EMP-004")
    old, new = meena.salary_assignments
    assert old.effective_to + timedelta(days=1) == new.effective_from
    assert (old.monthly_gross, new.monthly_gross) == (Decimal("50000"), Decimal("55000"))


def test_arrear_matches_salary_history(seeded_session):
    meena = _employee(seeded_session, "EMP-004")
    old, new = meena.salary_assignments
    old_amounts = {c.head_code: c.monthly_amount for c in old.components}
    new_amounts = {c.head_code: c.monthly_amount for c in new.components}
    arrears = seeded_session.scalars(select(Arrear).where(Arrear.employee_id == meena.id))
    for arrear in arrears:
        assert arrear.old_monthly_amount == old_amounts[arrear.head_code]
        assert arrear.new_monthly_amount == new_amounts[arrear.head_code]


def _sundays(start: date, end: date) -> int:
    return sum(1 for n in range((end - start).days + 1) if (start + timedelta(n)).weekday() == 6)


def test_attendance_is_internally_consistent(seeded_session):
    for row in seeded_session.scalars(select(Attendance)):
        employee = seeded_session.get(Employee, row.employee_id)
        period = PayrollPeriod.parse(row.period)
        assert row.calendar_days == calendar.monthrange(period.year, period.month)[1]
        start = max(period.start, employee.doj)
        end = min(period.end, employee.lwd or period.end)
        employed = (end - start).days + 1
        assert row.week_offs == _sundays(start, end), (employee.employee_code, row.period)
        assert row.working_days + row.week_offs + row.holidays == employed
        assert row.present_days + row.paid_leave_days + row.absent_days == row.working_days
        assert row.lop_days <= row.absent_days


def test_bad_components_rejected(tmp_path, session):
    data = read_yaml(DEFAULT_SEED_FILE)
    data["employees"][0]["salary"][0]["components"]["BASIC"] = 1
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(SeedDataError, match="components add up to"):
        seed_demo_data(session, path)


def test_unknown_field_rejected(tmp_path, session):
    data = read_yaml(DEFAULT_SEED_FILE)
    data["employees"][0]["salary_band"] = "B2"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    with pytest.raises(SeedDataError, match="unknown field"):
        seed_demo_data(session, path)
