"""Payroll Runs screen backend: months, month detail, pre-checks."""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.db.models import Attendance, PayrollRun
from payroll_copilot.services import payroll_runs
from tests.unit.batch.test_lifecycle import SEP, calculated_batch, emp
from tests.unit.chat.conftest import FakeChatModel


def check(result, key):
    return next(c for c in result["checks"] if c["key"] == key)


def test_months_and_detail_after_a_run(seeded_engine):
    calculated_batch(seeded_engine, work_state="KA")
    from payroll_copilot.db.session import get_session_factory

    with get_session_factory(seeded_engine)() as s:
        months = {m["period"]: m for m in payroll_runs.list_months(s)}
        sep = months["2026-09"]
        runs = s.scalars(select(PayrollRun).where(PayrollRun.period == "2026-09")).all()
        assert sep["employees"] == len(runs) > 0 and sep["status"] == "COMPLETED"
        assert Decimal(sep["net"]) == sum(r.net_pay for r in runs)
        detail = payroll_runs.month_detail(s, SEP)
        assert detail["batches"][0]["done"] == len(runs)
        assert detail["batches"][0]["created_by"] == "Anjali Mehta"


def test_prechecks_find_real_problems(seeded_session):
    s = seeded_session
    ok = payroll_runs.prechecks(s, SEP)
    assert ok["employees"] > 0 and ok["can_run"]
    assert check(ok, "SALARY")["status"] == "PASS"
    assert any("PF rule changes" in n["message"] for n in check(ok, "PF")["notes"])

    e = emp(s, "EMP-104")
    e.pf_applicable_date = None
    e.pan = None
    for row in s.scalars(select(Attendance).where(Attendance.employee_id == e.id)):
        s.delete(row)
    s.flush()
    bad = payroll_runs.prechecks(s, SEP)
    pf, att, tds = check(bad, "PF"), check(bad, "ATTENDANCE"), check(bad, "TDS")
    assert pf["status"] == "FAIL" and pf["failed"] == 1
    assert any(i["employee_code"] == "EMP-104" and "Applicable Date" in i["message"]
               for i in pf["issues"])
    assert att["status"] == "FAIL" and any(
        i["employee_code"] == "EMP-104" and "No attendance" in i["message"] for i in att["issues"])
    assert any("PAN missing" in i["message"] for i in tds["issues"])
    assert bad["can_run"]  # employee-level failures fail only those employees


def test_prechecks_scope_and_http(seeded_engine, make_settings):
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip

    def auth(email, password):
        token = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    hr, employee = auth("anjali.mehta@example.com", "Hr@12345"), auth(
        "rahul.verma@example.com", "Employee@123")
    assert client.get("/payroll-runs", headers=employee).status_code == 403
    assert client.get("/payroll-runs", headers=hr).status_code == 200
    one = client.get("/payroll-runs/2026-09/prechecks?work_state=Karnataka", headers=hr).json()
    allp = client.get("/payroll-runs/2026-09/prechecks", headers=hr).json()
    assert 0 < one["employees"] < allp["employees"] and one["filters"] == {"work_state": "KA"}
    assert client.get("/payroll-runs/2026-09/prechecks?work_state=Narnia",
                      headers=hr).status_code == 422
    assert client.get("/payroll-runs/2026-09", headers=hr).json()["period"] == "2026-09"
