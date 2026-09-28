"""Maker-checker approvals: request → preview → approve/reject → execute (or fail safely)."""

from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from payroll_copilot.audit.context import audit_context
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Approval, Attendance, AuditLog, PFRule, QARun, TDSOverride
from payroll_copilot.domain.enums import RuleStatus
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.rules.loader import load_rule_files, sync_rules
from payroll_copilot.services.actual_payroll_seed import seed_actual_payroll
from payroll_copilot.services.approval_service import (
    ApprovalError,
    ApprovalPermissionError,
    ApprovalService,
    InvalidApprovalRequest,
)
from payroll_copilot.services.tds_service import TDSService
from tests.conftest import RULES_DIR

SEP = PayrollPeriod(2026, 9)
D = Decimal
TDS = {"employee_code": "EMP-009", "period": "2026-09", "amount": "3000",
       "reason": "Employee asked for higher TDS"}  # fmt: skip


@pytest.fixture
def service(seeded_session):
    return ApprovalService(seeded_session, approvers=["alice", "bob"])


def attendance(session, code="EMP-002"):
    employee = EmployeeRepository(session).get_by_code(code)
    return session.scalar(select(Attendance).where(Attendance.employee_id == employee.id,
                                                   Attendance.period == "2026-09"))  # fmt: skip


class TestRequest:
    def test_tds_override_preview(self, service):
        view = service.request("TDS_OVERRIDE", TDS, requested_by="cli:carol")
        assert view.status == "PENDING" and view.requested_by == "cli:carol"
        assert view.preview == {"current_monthly_tds": "2427", "new_monthly_tds": "3000",
                                "difference": "573"}  # fmt: skip
        assert view.description == "Override monthly TDS of EMP-009 for 2026-09 to ₹3000"

    @pytest.mark.parametrize(("action", "payload", "message"), [
        ("TDS_OVERRIDE", {**TDS, "amount": "-1"}, "amount"),
        ("TDS_OVERRIDE", {**TDS, "reason": "x"}, "reason"),
        ("TDS_OVERRIDE", {**TDS, "employee_code": "EMP 9"}, "employee_code"),
        ("DELETE_EVERYTHING", {}, "Unknown action"),
    ])  # fmt: skip
    def test_invalid(self, service, action, payload, message):
        with pytest.raises(InvalidApprovalRequest, match=message):
            service.request(action, payload, requested_by="cli:carol")

    def test_preview_errors_block_the_request(self, service):
        with pytest.raises(Exception, match="not found"):
            service.request("TDS_OVERRIDE", {**TDS, "employee_code": "EMP-999"},
                            requested_by="cli:carol")  # fmt: skip


class TestDecisions:
    def test_approve_executes_and_tds_uses_it(self, service, seeded_session):
        request = service.request("TDS_OVERRIDE", TDS, requested_by="cli:carol")
        done = service.approve(request.id, "alice", "form signed")
        assert done.status == "EXECUTED" and done.decided_by == "alice"
        assert done.result["amount"] == "3000"
        result = TDSService(seeded_session).calculate("EMP-009", SEP)
        assert result.monthly_tds == D(3000) and result.overridden
        assert "approved override" in result.trace.to_text()

    def test_new_override_replaces_old(self, service, seeded_session):
        for amount, approver in (("3000", "alice"), ("3500", "bob")):
            r = service.request("TDS_OVERRIDE", {**TDS, "amount": amount},
                                requested_by="cli:carol")  # fmt: skip
            service.approve(r.id, approver)
        active = seeded_session.scalars(select(TDSOverride).where(TDSOverride.active)).all()
        assert [o.amount for o in active] == [D(3500)]
        assert TDSService(seeded_session).calculate("EMP-009", SEP).monthly_tds == D(3500)

    @pytest.mark.parametrize(("approvers", "approver", "message"), [
        ([], "alice", "No approvers configured"),
        (["alice"], "mallory", "not an approver"),
        (["alice"], "alice", "Maker-checker"),       # requested by cli:alice
        (["alice"], "api:alice", "Maker-checker"),   # same person, other channel
    ])  # fmt: skip
    def test_permissions(self, seeded_session, approvers, approver, message):
        service = ApprovalService(seeded_session, approvers=approvers)
        request = service.request("TDS_OVERRIDE", TDS, requested_by="cli:alice")
        with pytest.raises(ApprovalPermissionError, match=message):
            service.approve(request.id, approver)
        assert service.get(request.id).status == "PENDING"

    def test_reject_needs_comment_and_is_final(self, service, seeded_session):
        request = service.request("TDS_OVERRIDE", TDS, requested_by="cli:carol")
        with pytest.raises(ApprovalError, match="needs a comment"):
            service.reject(request.id, "alice", "")
        rejected = service.reject(request.id, "alice", "no signed form")
        assert rejected.status == "REJECTED"
        with pytest.raises(ApprovalError, match="already REJECTED"):
            service.approve(request.id, "bob")
        assert seeded_session.scalar(select(func.count()).select_from(TDSOverride)) == 0

    def test_list_and_get(self, service):
        a = service.request("TDS_OVERRIDE", TDS, requested_by="cli:carol")
        service.request("SAVE_QA_RUN", {"period": "2026-09"}, requested_by="cli:carol")
        service.approve(a.id, "alice")
        assert [v.status for v in service.list()] == ["PENDING", "EXECUTED"]
        assert len(service.list("pending")) == 1
        assert "Approval #1 [EXECUTED]" in service.get(a.id).to_text()


class TestActions:
    def test_attendance_correction(self, service, seeded_session):
        request = service.request("ATTENDANCE_CORRECTION", {
            "employee_code": "EMP-002", "period": "2026-09", "lop_days": "2",
            "reason": "one leave was approved late"}, requested_by="cli:carol")  # fmt: skip
        assert request.preview == {"lop_days_before": "3", "lop_days_after": "2",
                                   "paid_days_before": "27", "paid_days_after": "28"}  # fmt: skip
        assert service.approve(request.id, "alice").status == "EXECUTED"
        row = attendance(seeded_session)
        assert row.lop_days == D(2) and row.source == f"APPROVAL#{request.id}"

    def test_stale_data_fails_safely(self, service, seeded_session):
        request = service.request("ATTENDANCE_CORRECTION", {
            "employee_code": "EMP-002", "period": "2026-09", "lop_days": "2",
            "reason": "one leave was approved late"}, requested_by="cli:carol")  # fmt: skip
        attendance(seeded_session).lop_days = D(4)  # someone else changed it meanwhile
        seeded_session.flush()
        done = service.approve(request.id, "alice")
        assert done.status == "FAILED" and "changed since the request" in done.result["error"]
        assert attendance(seeded_session).lop_days == D(4)

    def test_save_qa_run(self, service, seeded_session):
        seed_actual_payroll(seeded_session)
        request = service.request("SAVE_QA_RUN", {"period": "2026-09"}, requested_by="cli:carol")
        assert request.preview["findings"] == 8
        done = service.approve(request.id, "alice")
        run = seeded_session.get(QARun, done.result["qa_run_id"])
        assert run.issues_found == 8
        log = seeded_session.scalar(select(AuditLog).where(AuditLog.action == "qa.save"))
        assert log.approval_id == request.id

    def test_verify_rule_survives_reload(self, service, seeded_session):
        request = service.request("VERIFY_RULE", {"rule_code": "epf", "version": 2,
                                                  "evidence": "EPFO notification 2026/09"},
                                  requested_by="cli:carol")  # fmt: skip
        assert request.preview["status_before"] == "UNVERIFIED"
        service.approve(request.id, "alice")
        rule = seeded_session.scalar(select(PFRule).where(PFRule.version == 2))
        assert rule.status is RuleStatus.VERIFIED and "EPFO notification" in rule.source_ref
        sync_rules(seeded_session, load_rule_files(RULES_DIR))  # YAML still says UNVERIFIED
        assert rule.status is RuleStatus.VERIFIED
        with pytest.raises(ApprovalError, match="is VERIFIED"):
            service.request("VERIFY_RULE", {"rule_code": "EPF", "version": 2,
                                            "evidence": "again please"}, requested_by="cli:x")

    def test_execution_error_rolls_back(self, service, seeded_session, monkeypatch):
        request = service.request("TDS_OVERRIDE", TDS, requested_by="cli:carol")
        original = ApprovalService._execute

        def explode(self, action, approval):
            original(self, action, approval)  # writes the override ...
            raise RuntimeError("disk full")  # ... then fails

        monkeypatch.setattr(ApprovalService, "_execute", explode)
        done = service.approve(request.id, "alice")
        assert done.status == "FAILED" and "disk full" in done.result["error"]
        assert seeded_session.scalar(select(func.count()).select_from(TDSOverride)) == 0


def test_audit_trail(service, seeded_session):
    with audit_context(actor="cli:carol"):
        request = service.request("TDS_OVERRIDE", TDS, requested_by="cli:carol")
    with audit_context(actor="api:alice"):
        service.approve(request.id, "alice")
    rows = seeded_session.scalars(select(AuditLog).where(AuditLog.approval_id == request.id)
                                  .order_by(AuditLog.id)).all()  # fmt: skip
    assert [(r.action, r.actor) for r in rows] == [("approval.request", "cli:carol"),
                                                   ("approval.execute", "api:alice")]  # fmt: skip
    assert seeded_session.get(Approval, request.id).executed_at is not None


class TestChatInterrupt:
    @pytest.fixture
    def copilot(self, seeded_session, kb, monkeypatch):
        from payroll_copilot.agent.copilot import PayrollCopilot
        from payroll_copilot.tools import ToolContext

        @contextmanager
        def sessions():
            yield seeded_session

        return PayrollCopilot(ToolContext(sessions, knowledge_base=lambda: kb),
                              use_configured_llm=False)  # fmt: skip

    def test_confirm_creates_pending_request_only(self, copilot, seeded_session):
        first = copilot.ask("EMP-009 ka TDS 2026-09 ke liye 3000 override karo", "c1",
                            actor="api:carol")  # fmt: skip
        assert first.route == "action_pending" and first.pending_action["action"] == "TDS_OVERRIDE"
        assert "current_monthly_tds: 2427" in first.answer and "(yes / no)" in first.answer
        unclear = copilot.ask("hmm maybe", "c1")
        assert unclear.route == "action_pending" and "'yes'" in unclear.answer
        done = copilot.ask("haan", "c1", actor="api:carol")
        assert "Approval request #1 ban gaya (PENDING)" in done.answer
        approval = seeded_session.get(Approval, 1)
        assert (approval.status.value, approval.requested_by) == ("PENDING", "api:carol")
        assert seeded_session.scalar(select(func.count()).select_from(TDSOverride)) == 0
        after = copilot.ask("ARIF-112 ka PF 2026-09", "c1")
        assert after.tools_used == ["calculate_pf"]

    def test_no_cancels(self, copilot, seeded_session):
        copilot.ask("correct LOP of EMP-002 for 2026-09 to 2 because late leave approval", "c2")
        cancel = copilot.ask("no", "c2")
        assert "cancel" in cancel.answer
        assert seeded_session.scalar(select(func.count()).select_from(Approval)) == 0

    @pytest.mark.parametrize(("question", "needle"), [
        ("override TDS for EMP-009 in 2026-09", "kitna TDS"),
        ("verify rule EPF v2", "evidence"),
        ("verify rule XYZ v9 as per circular 12", "not found"),
    ])  # fmt: skip
    def test_clarify_or_explain(self, copilot, question, needle):
        reply = copilot.ask(question, question)
        assert needle in reply.answer and reply.pending_action is None


def test_api_approvals(seeded_session, kb, make_settings):
    from fastapi.testclient import TestClient

    from payroll_copilot.agent.copilot import PayrollCopilot
    from payroll_copilot.api.main import create_app

    @contextmanager
    def sessions():
        yield seeded_session

    client = TestClient(create_app(
        make_settings(approvers=["alice"]), sessions=sessions, knowledge_base=lambda: kb,
        copilot_factory=lambda ctx: PayrollCopilot(ctx, use_configured_llm=False),
    ))  # fmt: skip
    body = {"action_type": "TDS_OVERRIDE", "payload": TDS}
    assert client.post("/approvals", json=body).status_code == 401  # who is asking?
    created = client.post("/approvals", json=body, headers={"X-User": "carol"})
    assert created.status_code == 201 and created.json()["requested_by"] == "api:carol"
    approval_id = created.json()["id"]
    bad = client.post("/approvals", json={"action_type": "NOPE", "payload": {}},
                      headers={"X-User": "carol"})  # fmt: skip
    assert bad.status_code == 422

    assert client.post(f"/approvals/{approval_id}/approve", json={},
                       headers={"X-User": "carol"}).status_code == 403  # fmt: skip
    ok = client.post(f"/approvals/{approval_id}/approve", json={"comment": "ok"},
                     headers={"X-User": "alice"})  # fmt: skip
    assert ok.status_code == 200 and ok.json()["status"] == "EXECUTED"
    again = client.post(f"/approvals/{approval_id}/reject", json={"comment": "late"},
                        headers={"X-User": "alice"})  # fmt: skip
    assert again.status_code == 409
    assert client.get("/approvals", params={"status": "EXECUTED"}).json()[0]["id"] == approval_id
    assert client.get("/approvals/999").status_code == 404

    chat = client.post("/chat", json={"message": "save QA report for 2026-09", "thread_id": "a1"},
                       headers={"X-User": "carol"}).json()  # fmt: skip
    assert chat["pending_action"]["action"] == "SAVE_QA_RUN"
