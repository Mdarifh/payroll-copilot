"""POST /chat and payslip endpoints over HTTP, with a scripted model."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.db.models import AuditLog
from tests.unit.chat.conftest import TODAY, FakeChatModel, call, say


def client_with(seeded_engine, make_settings, model) -> TestClient:
    return TestClient(create_app(make_settings(), engine=seeded_engine, chat_model=model,
                                 today=lambda: TODAY))  # fmt: skip


def token(client, email, password="Employee@123") -> dict[str, str]:
    r = client.post("/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def rahul():
    return "rahul.verma@example.com"


def test_chat_needs_login(seeded_engine, make_settings):
    client = client_with(seeded_engine, make_settings, FakeChatModel())
    assert client.post("/chat", json={"message": "hi"}).status_code == 401


def test_chat_payslip_flow(seeded_engine, make_settings, seeded_session, rahul):
    model = FakeChatModel(call("get_payslip", month="2026-08"), say("Net pay ₹58,000."),
                          say("You're welcome!"))  # fmt: skip
    client = client_with(seeded_engine, make_settings, model)
    auth = token(client, rahul)
    r = client.post("/chat", json={"message": "August?"}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert [m["type"] for m in body["messages"]] == ["text", "payslip_card"]
    assert body["tools_used"] == ["get_payslip"] and body["model"] == "fake:scripted"
    conv = body["conversation_id"]
    follow = client.post("/chat", json={"message": "thanks", "conversation_id": conv},
                         headers=auth).json()  # fmt: skip
    assert follow["conversation_id"] == conv
    assert len(model.calls[2]["history"]) == 5  # user, tool call, result, answer, user
    rows = seeded_session.scalars(
        select(AuditLog).where(AuditLog.action != "payroll.run").order_by(AuditLog.id))
    actions = [a.action for a in rows]
    assert actions == ["auth.login", "chat.tool", "chat.ask", "chat.ask"]


def test_chat_validation(seeded_engine, make_settings, rahul):
    client = client_with(seeded_engine, make_settings, FakeChatModel())
    auth = token(client, rahul)
    assert client.post("/chat", json={"message": ""}, headers=auth).status_code == 422
    assert client.post("/chat", json={"message": "x" * 2001}, headers=auth).status_code == 422
    bad_id = {"message": "hi", "conversation_id": "../../etc"}
    assert client.post("/chat", json=bad_id, headers=auth).status_code == 422


def test_llm_down_is_503(seeded_engine, make_settings, rahul):
    class Down(FakeChatModel):
        def _generate(self, *args, **kwargs):
            raise ConnectionError("Failed to connect to Ollama")  # what the ollama client raises

    client = client_with(seeded_engine, make_settings, Down())
    r = client.post("/chat", json={"message": "hi"}, headers=token(client, rahul))
    assert r.status_code == 503 and "not reachable" in r.json()["detail"]


def test_tools_listing_per_role(seeded_engine, make_settings, rahul):
    client = client_with(seeded_engine, make_settings, FakeChatModel())
    emp = {t["name"] for t in client.get("/chat/tools", headers=token(client, rahul)).json()}
    hr_auth = token(client, "anjali.mehta@example.com", "Hr@12345")
    hr = {t["name"] for t in client.get("/chat/tools", headers=hr_auth).json()}
    assert "get_attendance_exceptions" in hr - emp


def test_payslip_endpoint_is_own_only(seeded_engine, make_settings, rahul):
    client = client_with(seeded_engine, make_settings, FakeChatModel())
    auth = token(client, rahul)
    slip = client.get("/payslips/2026-08", headers=auth).json()
    assert slip["employee_code"] == "EMP-101" and slip["net_pay"] == "58000"
    assert client.get("/payslips/2026-09", headers=auth).status_code == 404
    assert client.get("/payslips/garbage", headers=auth).status_code == 422


def test_forget_conversation(seeded_engine, make_settings, rahul):
    client = client_with(seeded_engine, make_settings, FakeChatModel(say("hi")))
    auth = token(client, rahul)
    conv = client.post("/chat", json={"message": "hi"}, headers=auth).json()["conversation_id"]
    assert client.delete(f"/chat/{conv}", headers=auth).json()["cleared"] is True
    assert client.delete(f"/chat/{conv}", headers=auth).json()["cleared"] is False


def test_payslip_pdf_download(seeded_engine, make_settings, seeded_session, rahul):
    client = client_with(seeded_engine, make_settings, FakeChatModel())
    auth = token(client, rahul)
    r = client.get("/payslips/2026-08/pdf", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-")
    assert 'filename="payslip_EMP-101_2026-08.pdf"' in r.headers["content-disposition"]
    assert client.get("/payslips/2026-09/pdf", headers=auth).status_code == 404
    assert client.get("/payslips/2026-08/pdf").status_code == 401
    downloads = seeded_session.scalars(
        select(AuditLog).where(AuditLog.action == "payslip.download")).all()
    assert len(downloads) == 1 and downloads[0].actor_role == "employee"


def test_pdf_text_has_the_payslip_numbers():
    import re
    import zlib

    from payroll_copilot.services.payslip_pdf import render_payslip_pdf

    line = {"head_code": "BASIC", "name": "Basic", "amount": "125000"}
    slip = {"employee_code": "E-1", "employee_name": "A B", "designation": None,
            "department": None, "period": "2026-08", "period_label": "Aug-2026",
            "paid_days": "31", "basis_days": "31", "lop_days": "0", "earnings": [line],
            "deductions": [], "employer_contributions": [], "gross_earnings": "125000",
            "total_deductions": "0", "net_pay": "125000", "monthly_ctc": "125000",
            "rule_versions": ["EPF v2"]}  # fmt: skip
    pdf = render_payslip_pdf(slip)
    text = b" ".join(zlib.decompress(m) for m in re.findall(rb"stream\r?\n(.*?)\r?\nendstream",
                                                             pdf, re.S))  # fmt: skip
    assert b"Rs. 1,25,000" in text and b"NET PAY" in text and b"Aug-2026" in text
