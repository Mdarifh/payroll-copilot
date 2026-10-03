"""Phase 3 endpoints: overtime overview and the what-if preview (read-only)."""

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from payroll_copilot.api.main import create_app
from payroll_copilot.db.models import AuditLog, LopRecord, OvertimeEntry
from payroll_copilot.db.session import get_session_factory
from tests.unit.chat.conftest import FakeChatModel


def client_for(engine, settings):
    client = TestClient(create_app(settings, engine=engine, chat_model=FakeChatModel(),
                                   worker=None))  # fmt: skip

    def auth(email, password):
        token = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    return client, auth("anjali.mehta@example.com", "Hr@12345"), auth(
        "rahul.verma@example.com", "Employee@123")


def test_simulate_is_hr_only_and_saves_nothing(seeded_engine, make_settings):
    client, hr, employee = client_for(seeded_engine, make_settings())
    body = {"employee_code": "EMP-101", "month": "September 2026",
            "change": {"lop_days": "2", "overtime": [{"work_date": "2026-09-12", "hours": "3"}]}}

    def rows():
        with get_session_factory(seeded_engine)() as s:
            return tuple(s.scalar(select(func.count()).select_from(m))
                         for m in (OvertimeEntry, LopRecord, AuditLog))

    assert client.post("/payroll/simulate", json=body, headers=employee).status_code == 403
    before = rows()
    r = client.post("/payroll/simulate", json=body, headers=hr)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["saved"] is False and {x["key"] for x in data["rows"]} >= {"net", "PF", "TDS"}
    assert rows()[:2] == before[:2]  # only login audit rows were added

    bad = {**body, "change": {"overtime": [{"work_date": "2026-09-12", "hours": "12"}]}}
    r = client.post("/payroll/simulate", json=bad, headers=hr)
    assert r.status_code == 422 and "per day" in r.json()["detail"]
    r = client.post("/payroll/simulate", json={**body, "change": {"sql": "DROP TABLE"}},
                    headers=hr)  # fmt: skip
    assert r.status_code == 422  # unknown fields are refused


def test_overtime_overview_visibility(seeded_engine, make_settings):
    client, hr, employee = client_for(seeded_engine, make_settings())
    mine = client.get("/employees/EMP-101/overtime?month=2026-09", headers=employee).json()
    assert not mine["restricted"] and mine["rate"]["rate_per_hour"]
    assert "Overtime policy" in mine["rate"]["source"]
    assert client.get("/employees/EMP-102/overtime", headers=employee).status_code == 403
    assert client.get("/employees/EMP-102/overtime?month=2026-09", headers=hr).status_code == 200
