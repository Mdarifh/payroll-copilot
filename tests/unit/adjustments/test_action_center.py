"""Phase 7: Action Center - submit, maker-checker approve, reject, withdraw, failures."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from payroll_copilot.api.main import create_app
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PermissionDeniedError
from payroll_copilot.db.models import Employee, LopRecord, PayrollRun
from payroll_copilot.services import action_center as ac
from payroll_copilot.services.company_config import set_setting
from payroll_copilot.services.payslip import process_payroll
from payroll_copilot.services.simulation import PayrollChange, simulate
from tests.unit.chat.conftest import FakeChatModel

SEP = PayrollPeriod(2026, 9)


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def submit(s, actor, change, code="EMP-101"):
    pid = simulate(s, actor, code, SEP, change)["preview_id"]
    return ac.submit(s, actor, code, SEP, change, pid)


LOP2 = PayrollChange(lop_days=Decimal(2), reason="Absent 14th and 15th, confirmed")


def test_change_waits_for_a_second_hr_user(seeded_session):
    s = seeded_session
    maker, checker = emp(s, "HR-001"), emp(s, "HR-002")
    process_payroll(s, emp(s, "EMP-101"), SEP, run_by=maker)
    out = submit(s, maker, LOP2)
    assert out["status"] == "PENDING_APPROVAL" and out["title"] == "LOP 0 → 2 days"
    assert s.scalar(select(func.count()).select_from(LopRecord)) == 0  # nothing applied yet
    with pytest.raises(PermissionDeniedError, match="different HR user"):
        ac.approve(s, maker, out["id"], ac.fresh_preview(s, maker, out["id"])["preview_id"])
    fresh = ac.fresh_preview(s, checker, out["id"])
    done = ac.approve(s, checker, out["id"], fresh["preview_id"])
    assert done["status"] == "APPLIED" and done["decided_by"] == "Rohit Kapoor"
    assert done["result"]["recalculated"] == ["2026-09"]
    run = s.scalar(select(PayrollRun).where(PayrollRun.period == "2026-09"))
    assert run.lop_days == 2 and run.version == 2
    with pytest.raises(ac.ActionConflictError):
        ac.approve(s, checker, out["id"], fresh["preview_id"])  # decided once only


def test_without_approval_setting_it_applies_at_once(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    set_setting(s, "change_approval", False, SEP.start, hr)
    out = submit(s, hr, LOP2)
    assert out["status"] == "APPLIED" and out["decided_by"] is None
    assert s.scalar(select(LopRecord.days)) == 2


def test_reject_withdraw_and_failure(seeded_session):
    s = seeded_session
    maker, checker = emp(s, "HR-001"), emp(s, "HR-002")
    a = submit(s, maker, LOP2)
    rejected = ac.reject(s, checker, a["id"], "Attendance was regularised")
    assert rejected["status"] == "REJECTED" and rejected["decision_note"]
    b = submit(s, maker, PayrollChange(adjustments=[{"kind": "EARNING", "head_code": "BONUS",
                                                     "amount": "500"}], reason="Spot award"))
    assert ac.reject(s, maker, b["id"], "Raised by mistake")["status"] == "CANCELLED"
    # a request that no longer validates when approved becomes FAILED with the reason
    c = submit(s, maker, PayrollChange(overtime=[{"work_date": "2026-09-12", "hours": "8"}],
                                       reason="Weekend release"))
    fresh = ac.fresh_preview(s, checker, c["id"])
    run = process_payroll(s, emp(s, "EMP-101"), SEP, run_by=maker)
    from payroll_copilot.domain.enums import PayrollRunStatus

    run.status = PayrollRunStatus.LOCKED  # month finalized meanwhile
    s.flush()
    failed = ac.approve(s, checker, c["id"], fresh["preview_id"])
    assert failed["status"] == "FAILED" and "locked" in failed["error"]
    counts = ac.list_actions(s)["counts"]
    assert counts == {"PENDING_APPROVAL": 0, "APPLIED": 0, "REJECTED": 1, "FAILED": 1,
                      "CANCELLED": 1}


def test_http_flow(seeded_engine, make_settings):
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip

    def auth(email, password="Hr@12345"):
        token = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    maker, checker = auth("anjali.mehta@example.com"), auth("rohit.kapoor@example.com")
    employee = auth("rahul.verma@example.com", "Employee@123")
    body = {"employee_code": "EMP-101", "month": "2026-09",
            "change": {"lop_days": "1", "reason": "Absent on 15th, confirmed"}}
    preview = client.post("/payroll/simulate", json=body, headers=maker).json()
    r = client.post("/payroll/apply", json={**body, "preview_id": "0" * 24}, headers=maker)
    assert r.status_code == 422 and "preview" in r.json()["detail"]
    sent = client.post("/payroll/apply", json={**body, "preview_id": preview["preview_id"],
                                               "source": "chat"}, headers=maker).json()
    assert sent["status"] == "PENDING_APPROVAL" and sent["source"] == "chat"
    assert client.get("/actions", headers=employee).status_code == 403
    listed = client.get("/actions?status=pending_approval", headers=checker).json()
    assert listed["counts"]["PENDING_APPROVAL"] == 1 and listed["items"][0]["id"] == sent["id"]
    fresh = client.get(f"/actions/{sent['id']}/preview", headers=checker).json()
    assert client.post(f"/actions/{sent['id']}/approve", json={"preview_id": fresh["preview_id"]},
                       headers=maker).status_code == 403  # maker cannot approve
    ok = client.post(f"/actions/{sent['id']}/approve", json={"preview_id": fresh["preview_id"]},
                     headers=checker).json()
    assert ok["status"] == "APPLIED"
    again = client.post(f"/actions/{sent['id']}/approve", json={"preview_id": fresh["preview_id"]},
                        headers=checker)
    assert again.status_code == 409

