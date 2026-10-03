"""Run payroll only for selected employees."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.db.models import PayrollRun
from payroll_copilot.db.session import get_session_factory
from payroll_copilot.domain.enums import PayrollRunStatus
from payroll_copilot.services import pay_adjustments as adj
from payroll_copilot.services.batch_service import BatchConflictError
from payroll_copilot.services.batch_worker import BatchWorker
from payroll_copilot.services.payroll_actions import run_selected
from payroll_copilot.services.payslip import process_payroll
from tests.unit.batch.test_lifecycle import SEP, emp
from tests.unit.chat.conftest import FakeChatModel


def runs(s) -> dict[str, PayrollRun]:
    rows = s.execute(select(PayrollRun, PayrollRun.employee_id).where(
        PayrollRun.period == "2026-09")).all()
    codes = {e.id: e.employee_code for e in [emp(s, c) for c in ("EMP-101", "EMP-102", "EMP-103")]}
    return {codes.get(eid, eid): r for r, eid in rows}


def test_only_selected_employees_are_calculated(seeded_engine):
    factory = get_session_factory(seeded_engine)
    with factory() as s:
        hr = emp(s, "HR-001")
        process_payroll(s, emp(s, "EMP-101"), SEP, run_by=hr)  # already processed
        adj.add_adjustment(s, hr, emp(s, "EMP-101"), SEP, "EARNING", "BONUS", "800", "spot award")
        before_101 = runs(s)["EMP-101"].net_pay
        batch = run_selected(s, hr, SEP, ["emp-101", "EMP-102"], "Run for two employees")
        assert batch.total == 2 and batch.filters["employees"] == ["EMP-101", "EMP-102"]
        s.commit()
    BatchWorker(factory, retry_delay=0).run_until_idle()
    with factory() as s:
        r = runs(s)
        assert set(r) == {"EMP-101", "EMP-102"}  # EMP-103 and everyone else untouched
        assert r["EMP-101"].net_pay == before_101 + 800 and r["EMP-101"].version == 2
        assert r["EMP-102"].version == 1


def test_refusals(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    with pytest.raises(Exception, match="Unknown employee"):
        run_selected(s, hr, SEP, ["EMP-999"], "Run for one employee")
    with pytest.raises(Exception, match="Select at least one"):
        run_selected(s, hr, SEP, [" "], "Run for nobody")
    run = process_payroll(s, emp(s, "EMP-101"), SEP, run_by=hr)
    run.status = PayrollRunStatus.LOCKED
    s.flush()
    with pytest.raises(Exception, match="locked"):
        run_selected(s, hr, SEP, ["EMP-101"], "Run for a locked one")
    run_selected(s, hr, SEP, ["EMP-102"], "First selected run")
    with pytest.raises(BatchConflictError, match="still in"):
        run_selected(s, hr, SEP, ["EMP-103"], "Second while the first is queued")


def test_http(seeded_engine, make_settings):
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip

    def auth(email, password):
        token = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    hr, employee = auth("anjali.mehta@example.com", "Hr@12345"), auth(
        "rahul.verma@example.com", "Employee@123")
    body = {"employee_codes": ["EMP-103"], "reason": "Run for one employee"}
    assert client.post("/payroll-runs/2026-09/run-selected", json=body,
                       headers=employee).status_code == 403
    r = client.post("/payroll-runs/2026-09/run-selected", json=body, headers=hr)
    assert r.status_code == 202 and r.json()["total"] == 1 and r.json()["status"] == "QUEUED"


def test_lop_can_be_set_in_the_run(seeded_engine):
    from decimal import Decimal

    from payroll_copilot.db.models import AuditLog, LopRecord
    from payroll_copilot.services.pay_adjustments import AdjustmentError

    factory = get_session_factory(seeded_engine)
    with factory() as s:
        hr = emp(s, "HR-001")
        with pytest.raises(AdjustmentError, match="not selected"):
            run_selected(s, hr, SEP, ["EMP-101"], "Run with LOP", {"EMP-102": "1"})
        with pytest.raises(AdjustmentError, match="steps of 0.5"):
            run_selected(s, hr, SEP, ["EMP-101"], "Run with LOP", {"EMP-101": "0.3"})
        s.rollback()
        run_selected(s, hr, SEP, ["EMP-101", "EMP-102"], "Absent 2 days, confirmed",
                     {"emp-101": "2"})  # fmt: skip
        s.commit()
    BatchWorker(factory, retry_delay=0).run_until_idle()
    with factory() as s:
        record = s.scalar(select(LopRecord).where(LopRecord.employee_id == emp(s, "EMP-101").id))
        assert record.days == 2 and record.reason == "Absent 2 days, confirmed"
        assert runs(s)["EMP-101"].lop_days == Decimal(2)
        log = s.scalar(select(AuditLog).where(AuditLog.action == "batch.run_selected"))
        assert log.new_value["lop_days"] == {"EMP-101": "2"}
