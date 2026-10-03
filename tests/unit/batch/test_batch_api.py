"""Batch endpoints over HTTP (HR only), the chat tool, and the reliability helpers."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.chat.tools import ToolError, run_tool
from payroll_copilot.db.models import PayrollBatch
from payroll_copilot.services.reliability import RateLimiter, TransientError, with_retry
from tests.unit.chat.conftest import FakeChatModel


@pytest.fixture
def app(seeded_engine, make_settings):
    return create_app(make_settings(), engine=seeded_engine, chat_model=FakeChatModel())


def auth(client, email, password):
    token = client.post("/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {token.json()['access_token']}"}


@pytest.fixture
def hr(app):
    client = TestClient(app)
    return client, auth(client, "anjali.mehta@example.com", "Hr@12345")


def test_employees_cannot_use_batches(app):
    client = TestClient(app)
    emp = auth(client, "rahul.verma@example.com", "Employee@123")
    assert client.post("/batches", json={"month": "2026-09"}, headers=emp).status_code == 403
    assert client.get("/batches/whatever", headers=emp).status_code == 403


def test_review_edit_run_status_report(app, hr):
    client, headers = hr
    started = client.post("/batches", json={"month": "Sep 2026"}, headers=headers)
    assert started.status_code == 201
    batch_id = started.json()["batch_id"]
    assert started.json()["review_count"] == 2

    page = client.get(f"/batches/{batch_id}/lop-items?search=priya", headers=headers).json()
    assert [i["emp_id"] for i in page["items"]] == ["EMP-102"] and page["editable"]
    patched = client.patch(f"/batches/{batch_id}/lop-items/EMP-102", json={"lop_days": "1"},
                           headers=headers).json()  # fmt: skip
    assert patched["item"]["deduction"] == "1333"
    assert patched["totals"]["total_deduction"] == str(1333 + 2000)  # + Rahul 60000/30 × 1
    bad = client.patch(f"/batches/{batch_id}/lop-items/EMP-102", json={"lop_days": "5"},
                       headers=headers)  # fmt: skip
    assert bad.status_code == 422

    no_reason = client.post(f"/batches/{batch_id}/run", json={"mode": "APPLY"}, headers=headers)
    assert no_reason.status_code == 422
    body = {"mode": "APPLY", "reason": "Absent without leave"}
    first = client.post(f"/batches/{batch_id}/run", json=body, headers=headers)
    second = client.post(f"/batches/{batch_id}/run", json=body, headers=headers)  # double click
    assert first.status_code == 202 and first.json()["status"] == "QUEUED"
    assert second.status_code == 409 and "not started again" in second.json()["detail"]

    app.state.worker.run_until_idle()
    status = client.get(f"/batches/{batch_id}/status", headers=headers).json()
    assert (status["status"], status["processed"], status["percent"]) == ("COMPLETED", 12, 100)
    report = client.get(f"/batches/{batch_id}/report.csv", headers=headers)
    assert report.headers["content-type"].startswith("text/csv")
    lines = report.text.strip().splitlines()
    assert lines[0].startswith("batch_id,period,employee_code") and len(lines) == 13
    assert client.get("/batches/nope/status", headers=headers).status_code == 404
    retry = client.post(f"/batches/{batch_id}/retry-failed", headers=headers)
    assert retry.status_code == 409  # nothing failed


def test_chat_tool_returns_only_a_summary_and_a_card(seeded_session, seeded_engine):
    from datetime import date

    from payroll_copilot.chat.tools import ToolContext
    from payroll_copilot.db.models import Employee

    def ctx(code):
        user = seeded_session.scalar(select(Employee).where(Employee.employee_code == code))
        return ToolContext(session=seeded_session, user=user, today=date(2026, 10, 3))

    result = run_tool(ctx("HR-001"), "start_payroll_review",
                      {"month": "October 2026", "department": "Finance"})  # fmt: skip
    assert set(result.content) >= {"batch_id", "total", "ok_count", "review_count"}
    assert not any(isinstance(v, list) for v in result.content.values())  # no employee list
    card = result.cards[0]
    assert card["type"] == "bulk_lop_card" and card["batch_id"] == result.content["batch_id"]
    # committed in its own transaction: visible from a brand-new session
    from payroll_copilot.db.session import get_session_factory

    with get_session_factory(seeded_engine)() as other:
        assert other.scalar(select(PayrollBatch).where(
            PayrollBatch.batch_id == card["batch_id"])) is not None  # fmt: skip
    with pytest.raises(ToolError, match="not allowed for the employee role"):
        run_tool(ctx("EMP-101"), "start_payroll_review", {"month": "2026-09"})


def test_with_retry_backoff():
    calls, waits = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("busy")
        return "ok"

    assert with_retry(flaky, attempts=3, base_delay=0.5, sleep=waits.append) == "ok"
    assert waits == [0.5, 1.0]
    with pytest.raises(TransientError):
        with_retry(lambda: (_ for _ in ()).throw(TransientError("down")), attempts=2,
                   sleep=lambda _: None)  # fmt: skip
    with pytest.raises(ValueError):  # not retryable: raised at once
        with_retry(lambda: (_ for _ in ()).throw(ValueError("bad")), sleep=waits.append)
    assert waits == [0.5, 1.0]  # the ValueError was not retried


def test_rate_limiter_spaces_out_calls():
    clock = [0.0]
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    limiter = RateLimiter(rate=2, burst=2, clock=lambda: clock[0], sleep=sleep)
    for _ in range(6):
        limiter.acquire()
    # 2 calls free (burst), then one every 0.5 s
    assert slept == [0.5, 0.5, 0.5, 0.5] and clock[0] == 2.0
