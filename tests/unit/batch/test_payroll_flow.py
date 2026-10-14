"""Payroll run as a LangGraph workflow with HR approval (interrupts), over HTTP and chat."""

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, select

from payroll_copilot.api.main import create_app
from payroll_copilot.db.models import Attendance, AuditLog, Employee, LopRecord
from tests.unit.chat.conftest import FakeChatModel

SEP = "2026-09"


@pytest.fixture
def saver():
    return InMemorySaver()


@pytest.fixture
def app(seeded_engine, make_settings, saver):
    return create_app(make_settings(), engine=seeded_engine, chat_model=FakeChatModel(),
                      checkpointer=saver)  # fmt: skip


def auth(client, email, password="Hr@12345"):
    token = client.post("/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {token.json()['access_token']}"}


@pytest.fixture
def hr(app):
    client = TestClient(app)
    return client, auth(client, "anjali.mehta@example.com")


def resume(client, headers, flow_id, **body):
    return client.post(f"/payroll-flows/{flow_id}/resume", json=body, headers=headers)


def test_prechecks_review_approve_run_validate_summary(app, hr, seeded_engine):
    client, headers = hr
    card = client.post("/payroll-flows", json={"month": SEP}, headers=headers).json()
    assert card["type"] == "payroll_flow_card" and card["stage"] == "approval"
    assert card["blockers"] == [] and card["batch_id"] and card["label"] == "Sep-2026"
    assert {c["key"] for c in card["prechecks"]["checks"]} >= {"ATTENDANCE", "PF"}
    flow = card["flow_id"]

    # LOP needs a reason: refused before the graph moves on, the card still waits
    no_reason = resume(client, headers, flow, action="approve", mode="APPLY")
    assert no_reason.status_code == 422 and "reason" in no_reason.json()["detail"]
    assert client.get(f"/payroll-flows/{flow}", headers=headers).json()["stage"] == "approval"

    running = resume(client, headers, flow, action="approve", mode="APPLY",
                     reason="Absent without leave").json()  # fmt: skip
    assert running["stage"] == "running" and running["run"]["status"] == "QUEUED"
    early = resume(client, headers, flow, action="continue").json()
    assert early["stage"] == "running"  # the worker has not calculated anything yet
    assert resume(client, headers, flow, action="cancel").status_code == 422

    app.state.worker.run_until_idle()
    done = resume(client, headers, flow, action="continue").json()
    assert done["stage"] == "done" and done["run"]["finished"]
    assert done["validation"]["stage"] in ("VALIDATED", "VALIDATION_FAILED")
    assert done["totals"]["employees"] == done["run"]["done"] > 0
    assert float(done["totals"]["net"]) > 0
    again = resume(client, headers, flow, action="approve", mode="SKIP_ALL").json()
    assert again["stage"] == "done"  # a finished run is never started again

    with app.state.session_factory() as s:
        assert s.scalar(select(LopRecord).join(Employee, Employee.id == LopRecord.employee_id).where(
            Employee.employee_code == "EMP-101", LopRecord.period == SEP)).days > 0
        actions = set(s.scalars(select(AuditLog.action).where(AuditLog.entity_id == flow)))
        assert {"payroll_flow.start", "payroll_flow.decision"} <= actions


def test_failed_prechecks_block_until_fixed_and_rechecked(app, hr):
    client, headers = hr
    with app.state.session_factory() as s:
        rahul = s.scalar(select(Employee).where(Employee.employee_code == "EMP-101"))
        saved = [(a.day, a.status, a.leave_id, a.remarks) for a in s.scalars(select(Attendance).where(
            Attendance.employee_id == rahul.id, Attendance.day.between("2026-09-01",
                                                                      "2026-09-30")))]
        s.execute(delete(Attendance).where(Attendance.employee_id == rahul.id,
                                           Attendance.day.between("2026-09-01", "2026-09-30")))
        s.commit()
    card = client.post("/payroll-flows", json={"month": SEP}, headers=headers).json()
    assert card["stage"] == "approval" and card["batch_id"] is None
    blocker = card["blockers"][0]
    assert blocker["check"] == "Attendance" and blocker["employees"][0]["code"] == "EMP-101"
    refused = resume(client, headers, card["flow_id"], action="approve", mode="SKIP_ALL").json()
    assert refused["stage"] == "approval" and "Fix the failed pre-checks" in refused["error"]

    with app.state.session_factory() as s:
        s.add_all(Attendance(employee_id=rahul.id, day=d, status=st, leave_id=lv, remarks=rm)
                  for d, st, lv, rm in saved)  # fmt: skip
        s.commit()
    fixed = resume(client, headers, card["flow_id"], action="recheck").json()
    assert fixed["stage"] == "approval" and fixed["blockers"] == [] and fixed["batch_id"]
    assert fixed["error"] is None


def test_cancel_runs_nothing(app, hr):
    client, headers = hr
    card = client.post("/payroll-flows", json={"month": SEP}, headers=headers).json()
    cancelled = resume(client, headers, card["flow_id"], action="cancel").json()
    assert cancelled["stage"] == "cancelled"
    batch = client.get(f"/batches/{card['batch_id']}/status", headers=headers).json()
    assert batch["status"] == "REVIEW" and batch["done"] == 0


def test_a_paused_run_survives_an_api_restart(app, hr, seeded_engine, make_settings, saver):
    client, headers = hr
    flow = client.post("/payroll-flows", json={"month": SEP}, headers=headers).json()["flow_id"]
    restarted = TestClient(create_app(make_settings(), engine=seeded_engine,
                                      chat_model=FakeChatModel(), checkpointer=saver))
    headers = auth(restarted, "anjali.mehta@example.com")
    assert restarted.get(f"/payroll-flows/{flow}", headers=headers).json()["stage"] == "approval"
    assert restarted.get("/payroll-flows/nope", headers=headers).status_code == 404


def test_only_payroll_staff_can_run(app):
    client = TestClient(app)
    emp = auth(client, "rahul.verma@example.com", "Employee@123")
    assert client.post("/payroll-flows", json={"month": SEP}, headers=emp).status_code == 403


def test_chat_starts_the_flow_without_the_llm(app, hr):
    client, headers = hr
    for message in ("Run September 2026 payroll", "September 2026 ka payroll sab ka process karo"):
        reply = client.post("/chat", json={"message": message}, headers=headers).json()
        assert reply["model"] == "intent-router" and "payroll_flow" in reply["tools_used"]
        card = reply["messages"][1]
        assert card["type"] == "payroll_flow_card" and card["stage"] == "approval"
        assert "Approve & run" in reply["messages"][0]["text"]


def test_chat_runs_only_the_named_employees(app, hr, seeded_engine):
    from sqlalchemy import func
    from sqlalchemy.orm import Session

    from payroll_copilot.db.models import PayrollBatch

    client, headers = hr

    def batches():
        with Session(seeded_engine) as s:
            return s.scalar(select(func.count()).select_from(PayrollBatch))

    before = batches()
    for message in ("EMP-101 ka September 2026 payroll process karo",
                    "Rahul Verma ka September 2026 payroll chalao"):
        reply = client.post("/chat", json={"message": message}, headers=headers).json()
        assert reply["model"] == "intent-router" and "run_selected" in reply["tools_used"]
        text, card = reply["messages"][0]["text"], reply["messages"][1]
        assert card["type"] == "run_selected_card" and card["month"] == SEP
        assert [r["employee_code"] for r in card["rows"]] == ["EMP-101"]
        assert "only for Rahul Verma (EMP-101)" in text and "Nothing has run yet" in text
    reply = client.post("/chat", json={"message": "EMP-101 aur EMP-999 ka September 2026 "
                                       "payroll chalao"}, headers=headers).json()  # fmt: skip
    card = reply["messages"][1]
    assert [r["employee_code"] for r in card["rows"]] == ["EMP-101"]
    assert card["skipped"] == ["EMP-999"] and "EMP-999" in reply["messages"][0]["text"]
    # only unknown codes: an error, never the company-wide run
    reply = client.post("/chat", json={"message": "EMP-999 ka September 2026 payroll chalao"},
                        headers=headers).json()  # fmt: skip
    assert len(reply["messages"]) == 1 and "can't run payroll" in reply["messages"][0]["text"]
    assert batches() == before  # the card only proposes

    # the card's Run button: run-selected calculates just that employee
    run = client.post(f"/payroll-runs/{SEP}/run-selected", headers=headers,
                      json={"employee_codes": ["EMP-101"], "reason": "Chat test run"})
    assert run.status_code == 202 and run.json()["total"] == 1
    app.state.worker.run_until_idle()
    status = client.get(f"/batches/{run.json()['batch_id']}/status", headers=headers).json()
    assert (status["status"], status["processed"]) == ("COMPLETED", 1)


def test_payroll_tool_with_employee_codes_shows_the_selected_card(seeded_session):
    from datetime import date

    from payroll_copilot.chat.tools import ToolContext, ToolError, run_tool

    hr_user = seeded_session.scalar(select(Employee).where(Employee.employee_code == "HR-001"))
    ctx = ToolContext(session=seeded_session, user=hr_user, today=date(2026, 10, 3))
    result = run_tool(ctx, "start_payroll_review",
                      {"month": SEP, "employee_codes": ["emp-101", "EMP-102"]})
    card = result.cards[0]
    assert card["type"] == "run_selected_card"
    assert sorted(r["employee_code"] for r in card["rows"]) == ["EMP-101", "EMP-102"]
    assert "nothing has run yet" in result.content["next_step"]
    with pytest.raises(ToolError, match="No employee to run"):
        run_tool(ctx, "start_payroll_review", {"month": SEP, "employee_codes": ["EMP-999"]})
