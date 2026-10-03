"""Overview dashboard: company numbers for HR, personal numbers for everyone else."""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Employee, PayrollRun
from payroll_copilot.services.overview import overview
from payroll_copilot.services.payslip import process_payroll
from tests.unit.chat.conftest import FakeChatModel


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def test_hr_sees_company_totals_from_processed_payroll(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    for code in ("EMP-101", "EMP-102"):
        process_payroll(s, emp(s, code), PayrollPeriod(2026, 9), run_by=hr)
    s.flush()
    o = overview(s, hr)
    assert o["scope"] == "company" and o["headcount"]["active"] >= 3
    latest = o["latest_payroll"]
    runs = s.scalars(select(PayrollRun).where(PayrollRun.period == "2026-09")).all()
    assert latest["period"] == "2026-09" and latest["employees"] == len(runs)
    assert Decimal(latest["net"]) == sum(r.net_pay for r in runs)
    assert Decimal(o["statutory"]["PF"]) > 0
    assert any(a["action"] == "payroll.run" for a in o["activity"])


def test_employee_sees_only_personal(seeded_session):
    o = overview(seeded_session, emp(seeded_session, "EMP-101"))
    assert o["scope"] == "personal" and "headcount" not in o and o["team_size"] is None


def test_overview_over_http(seeded_engine, make_settings):
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip
    assert client.get("/overview").status_code == 401
    token = client.post("/auth/login", json={"email": "rahul.verma@example.com",
                                             "password": "Employee@123"}).json()["access_token"]
    r = client.get("/overview", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["scope"] == "personal"
