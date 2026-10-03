from contextlib import contextmanager

import pytest
from sqlalchemy import func, select

from payroll_copilot.audit import AuditLogger, audit_context, current_actor, rule_versions
from payroll_copilot.audit.context import current_request_id
from payroll_copilot.audit.redaction import redact
from payroll_copilot.db.models import AuditLog


@pytest.fixture
def logger(session):
    @contextmanager
    def sessions():
        yield session

    return AuditLogger(sessions)


class TestRedaction:
    def test_masks_pan_and_twelve_digit_ids(self):
        text = "PAN ABCDE1234F, UAN 100000000112, Aadhaar 123412341234, amount 25000"
        assert redact(text) == ("PAN ******234F, UAN ********0112, Aadhaar ********1234, "
                                "amount 25000")  # fmt: skip

    def test_masks_secrets_recursively_and_truncates(self):
        data = {"a": ["key sk-secret-1"], "b": {"c": "x" * 3000}, "n": 5}
        out = redact(data, ["sk-secret-1"])
        assert out["a"] == ["key ****"] and out["n"] == 5
        assert len(out["b"]["c"]) == 2001 and out["b"]["c"].endswith("…")


class TestContext:
    def test_default_actor_is_cli_user(self):
        assert current_actor().startswith("cli:") and current_request_id() is None

    def test_nested_context_restores(self):
        with audit_context(actor="api:alice") as outer:
            assert current_actor() == "api:alice" and current_request_id() == outer
            with audit_context(request_id="req-inner-1"):
                assert current_actor() == "api:alice" and current_request_id() == "req-inner-1"
            assert current_request_id() == outer
        assert current_request_id() is None


class TestLogger:
    def test_log_and_search(self, logger, session):
        with audit_context(actor="api:bob", request_id="req-12345678"):
            entry = logger.log("tool.call", tool_name="calculate_pf", entity="employee",
                               entity_id="ARIF-112", details={"pan": "ABCDE1234F"})  # fmt: skip
        logger.log("chat.ask", entity="thread", entity_id="t1", actor="cli:x")
        row = session.get(AuditLog, entry)
        assert (row.actor, row.request_id, row.after) == ("api:bob", "req-12345678",
                                                          {"pan": "******234F"})  # fmt: skip
        assert [r["action"] for r in logger.search()] == ["chat.ask", "tool.call"]
        assert len(logger.search(action="tool.*")) == 1
        assert logger.search(entity_id="t1")[0]["actor"] == "cli:x"
        assert logger.search(request_id="req-12345678")[0]["tool_name"] == "calculate_pf"

    def test_disabled(self, session):
        assert AuditLogger(lambda: None, enabled=False).log("x") is None

    def test_failures_ignored_for_reads_raised_for_writes(self, caplog):
        @contextmanager
        def broken():
            raise RuntimeError("db down")
            yield

        assert AuditLogger(broken).log("tool.call") is None
        assert "Audit write failed" in caplog.text
        with pytest.raises(RuntimeError):
            AuditLogger(broken).log("qa.save", strict=True)


def test_rule_versions():
    payload = {"traces": [{"rule": {"rule_code": "EPF", "version": 2, "effective_from": "x"}},
                          {"rule": {"rule_code": "EPF", "version": 2, "effective_from": "x"}}],
               "pt": {"rule": {"rule_code": "PT-MH", "version": 1, "effective_from": "y"}}}
    assert rule_versions(payload) == ["EPF v2", "PT-MH v1"]
    assert rule_versions({"x": 1}) == []


def test_tools_audit_every_call(seeded_session, kb):
    from payroll_copilot.tools import ToolContext, build_tools

    @contextmanager
    def sessions():
        yield seeded_session

    tools = {t.name: t for t in build_tools(ToolContext(sessions, knowledge_base=lambda: kb))}
    with audit_context(actor="api:carol", request_id="req-tools-01"):
        tools["calculate_pf"].invoke({"employee_code": "ARIF-112", "period": "2026-09"})
        tools["calculate_pf"].invoke({"employee_code": "EMP-999", "period": "2026-09"})
    rows = seeded_session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    assert [r.tool_name for r in rows] == ["calculate_pf", "calculate_pf"]
    assert rows[0].after["rule_versions"] == ["EPF v2"] and rows[0].after["ok"] is True
    assert rows[1].after["ok"] is False and "not found" in rows[1].after["error"]
    assert {r.actor for r in rows} == {"api:carol"}


def test_default_tool_context_audits_in_its_own_transaction():
    from payroll_copilot.db.session import session_scope
    from payroll_copilot.tools import ToolContext

    assert ToolContext().audit.sessions is session_scope


class TestCopilotMemoryAndAudit:
    @pytest.fixture
    def make_copilot(self, seeded_session, kb, tmp_path):
        from payroll_copilot.agent.copilot import PayrollCopilot, sqlite_checkpointer
        from payroll_copilot.tools import ToolContext

        @contextmanager
        def sessions():
            yield seeded_session

        path = tmp_path / "memory.db"

        def _make():
            return PayrollCopilot(ToolContext(sessions, knowledge_base=lambda: kb),
                                  use_configured_llm=False, checkpointer=sqlite_checkpointer(path))

        return _make

    def test_memory_survives_a_new_copilot(self, make_copilot):
        make_copilot().ask("ARIF-112 ka PF 2026-09", thread_id="p1")
        restarted = make_copilot()
        follow = restarted.ask("aur ESIC?", thread_id="p1")
        assert follow.entities["employee_source"] == "memory"
        assert restarted.memory("p1") == {"last_employee_code": "ARIF-112",
                                          "last_period": "2026-09", "turns": 2}  # fmt: skip
        history = restarted.history("p1")
        assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
        restarted.clear("p1")
        assert restarted.memory("p1")["turns"] == 0
        assert restarted.ask("aur ESIC?", thread_id="p1").route == "clarify"

    def test_chat_turn_and_tool_calls_share_request_id(self, make_copilot, seeded_session):
        reply = make_copilot().ask("ARIF-112 ka PF 2026-09", thread_id="p2", actor="api:dave")
        rows = seeded_session.scalars(
            select(AuditLog).where(AuditLog.request_id == reply.request_id)).all()  # fmt: skip
        assert {r.action for r in rows} == {"chat.ask", "tool.call"}
        chat = next(r for r in rows if r.action == "chat.ask")
        assert chat.actor == "api:dave" and chat.entity_id == "p2"
        assert chat.after["rule_versions"] == ["EPF v2"] and chat.after["tools"] == ["calculate_pf"]


def test_api_requests_are_audited(seeded_session, kb, make_settings):
    from fastapi.testclient import TestClient

    from payroll_copilot.agent.copilot import PayrollCopilot
    from payroll_copilot.api.main import create_app

    @contextmanager
    def sessions():
        yield seeded_session

    client = TestClient(create_app(
        make_settings(), sessions=sessions, knowledge_base=lambda: kb,
        copilot_factory=lambda ctx: PayrollCopilot(ctx, use_configured_llm=False),
    ))  # fmt: skip
    client.get("/health")
    r = client.post("/calculate/pf", json={"employee_code": "ARIF-112", "period": "2026-09"},
                    headers={"X-User": "alice", "X-Request-ID": "req-api-0001"})  # fmt: skip
    assert r.headers["X-Request-ID"] == "req-api-0001"
    client.get("/employees", headers={"X-User": "bad user!"})
    audit = client.get("/audit", params={"action": "api.request"}).json()
    paths = [a["details"]["path"] for a in audit]
    assert "/health" not in paths and "/calculate/pf" in paths
    pf = next(a for a in audit if a["details"]["path"] == "/calculate/pf")
    assert pf["actor"] == "api:alice" and pf["details"]["status"] == 200
    assert next(a for a in audit if a["details"]["path"] == "/employees")["actor"] == "api:anonymous"

    chat = client.post("/chat", json={"message": "ARIF-112 ka PF 2026-09", "thread_id": "w1"},
                       headers={"X-Request-ID": "req-chat-0001"})  # fmt: skip
    assert chat.json()["request_id"] == chat.headers["X-Request-ID"] == "req-chat-0001"
    linked = {a["action"] for a in client.get("/audit", params={"request_id": "req-chat-0001"})
              .json()}  # fmt: skip
    assert {"api.request", "chat.ask", "tool.call"} <= linked
    history = client.get("/chat/w1").json()
    assert history["memory"]["last_employee_code"] == "ARIF-112" and len(history["messages"]) == 2
    assert client.delete("/chat/w1").json()["cleared"]
    assert client.get("/chat/w1").json()["memory"]["turns"] == 0
    assert client.get("/chat/bad%20id").status_code == 422
    assert seeded_session.scalar(select(func.count()).select_from(AuditLog)) >= 6
