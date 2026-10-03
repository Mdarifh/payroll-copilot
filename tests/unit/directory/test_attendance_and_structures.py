"""Employees attendance tab + Salary Structures screen (Phase 2)."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import (
    NotFoundError,
    PayrollCopilotError,
    PermissionDeniedError,
)
from payroll_copilot.db.models import Employee
from payroll_copilot.services import employee_directory as directory
from payroll_copilot.services import salary_structures as structures
from tests.unit.chat.conftest import FakeChatModel

SEP = PayrollPeriod(2026, 9)


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def test_attendance_summary_matches_the_payroll_paid_days(seeded_session):
    s = seeded_session
    a = directory.attendance_details(s, emp(s, "HR-001"), "EMP-101", SEP)
    assert a["available"] and a["month"] == "2026-09"
    sm = a["summary"]
    assert len(a["days"]) == sm["calendar_days"] == 30
    statuses = [d["status"] for d in a["days"]]
    assert Decimal(sm["holidays"]) == statuses.count("HOLIDAY")
    assert Decimal(sm["week_offs"]) == statuses.count("WEEK_OFF")
    assert Decimal(sm["paid_days"]) == Decimal(sm["basis_days"]) - Decimal(sm["lop_days"])
    assert a["paid_days_steps"][-1]["label"] == "Paid days"


def test_attendance_visibility(seeded_session):
    s = seeded_session
    with pytest.raises(PermissionDeniedError):
        directory.attendance_details(s, emp(s, "EMP-101"), "EMP-102", SEP)
    mgr = emp(s, "MGR-001")
    member = s.scalar(select(Employee).where(Employee.manager_id == mgr.id))
    assert directory.attendance_details(s, mgr, member.employee_code, SEP)["available"]


def test_attendance_for_a_month_without_data(seeded_session):
    s = seeded_session
    a = directory.attendance_details(s, emp(s, "HR-001"), "EMP-101", PayrollPeriod(2026, 1))
    assert not a["available"] and "No attendance" in a["message"]


def test_structures_list_detail_employees(seeded_session):
    s = seeded_session
    listed = {i["code"]: i for i in structures.list_structures(s)["items"]}
    assert {"STD-2026", "LOW-2026"} <= listed.keys()
    d = structures.structure_detail(s, "STD-2026")
    assert [c["head_code"] for c in d["components"]][0] == "BASIC"
    hra = next(c for c in d["components"] if c["head_code"] == "HRA")
    assert hra["basis"] == "50% of Basic" and hra["tax_treatment"] == "Exempt with limit"
    people = structures.structure_employees(s, "STD-2026", page_size=100)
    assert people["total"] == listed["STD-2026"]["employees"] == len(people["items"])
    assert all(Decimal(p["monthly_gross"]) > 0 for p in people["items"])
    with pytest.raises(NotFoundError):
        structures.structure_detail(s, "NOPE")


def test_preview_uses_the_engine():
    p = structures.preview("STD-2026", "45000")
    lines = {ln["head_code"]: Decimal(ln["amount"]) for ln in p["lines"]}
    assert lines["BASIC"] == 22500 and lines["HRA"] == 11250
    assert sum(lines.values()) == Decimal(p["total"]) == 45000
    assert p["annual_gross"] == "540000"
    for bad in ("abc", "0", "-5", "999999999"):
        with pytest.raises(PayrollCopilotError):
            structures.preview("STD-2026", bad)


def test_structures_http_is_hr_only(seeded_engine, make_settings):
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip

    def auth(email, password):
        token = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    hr = auth("anjali.mehta@example.com", "Hr@12345")
    employee = auth("rahul.verma@example.com", "Employee@123")
    assert client.get("/structures", headers=hr).status_code == 200
    assert client.get("/structures", headers=employee).status_code == 403
    assert client.get("/structures/STD-2026/preview?gross=30000", headers=employee).status_code == 403
    r = client.get("/structures/STD-2026/preview?gross=30000", headers=hr)
    assert r.status_code == 200 and r.json()["total"] == "30000"
    assert client.get("/structures/STD-2026/preview?gross=abc", headers=hr).status_code in (400, 422)
    att = client.get("/employees/EMP-101/attendance?month=2026-09", headers=employee)
    assert att.status_code == 200 and att.json()["available"]
