"""Saved conversations and follow-up questions."""

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.chat.agent import ChatAgent
from payroll_copilot.chat.tools import ToolContext
from payroll_copilot.db.models import Employee
from tests.unit.chat.conftest import FakeChatModel, say

TODAY = date(2026, 10, 3)


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def ask(s, code, message, context):
    ctx = ToolContext(session=s, user=emp(s, code), today=TODAY, chat_context=context)
    reply = ChatAgent(FakeChatModel()).ask(ctx, message, "c")
    return reply, ctx.chat_context


def test_follow_up_questions_reuse_employee_month_and_topic(seeded_session):
    s = seeded_session
    reply, ctx = ask(s, "HR-001", "Rahul ka September PF kitna hai?", {})
    assert ctx == {"intent": "payroll_explanation", "employee": "EMP-101", "component": "PF",
                   "months": ["2026-09"]}
    reply, ctx = ask(s, "HR-001", "aur uska ESIC?", ctx)  # same person and month, new topic
    card = reply.messages[1]
    assert (card["component"], card["employee_code"], card["month"]) == ("ESIC", "EMP-101",
                                                                        "2026-09")
    reply, ctx = ask(s, "HR-001", "August ka bhi", ctx)  # same person and topic, new month
    assert (reply.messages[1]["component"], reply.messages[1]["month"]) == ("ESIC", "2026-08")
    reply, ctx = ask(s, "HR-001", "what about EMP-102", ctx)  # new person
    assert reply.messages[1]["employee_code"] == "EMP-102"


def test_follow_up_never_repeats_a_change_or_crosses_privacy(seeded_session):
    s = seeded_session
    _, ctx = ask(s, "HR-001", "Rahul ka LOP 2 days kar do", {})
    assert ctx == {}  # a change is never kept as context
    _, ctx = ask(s, "EMP-101", "mera September PF kaise bana", {})
    reply, _ = ask(s, "EMP-101", "what about EMP-102", ctx)
    assert "own payroll" in reply.messages[0]["text"] and len(reply.messages) == 1


def test_history_is_saved_per_user_and_survives_a_restart(seeded_engine, make_settings):
    def client_with(model):
        return TestClient(create_app(make_settings(), engine=seeded_engine, chat_model=model,
                                     worker=None))  # fmt: skip

    def auth(client, email, password):
        token = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    first = client_with(FakeChatModel(say("Hello Rahul!")))
    rahul = auth(first, "rahul.verma@example.com", "Employee@123")
    conv = first.post("/chat", json={"message": "hi there"}, headers=rahul).json()[
        "conversation_id"]
    first.post("/chat", json={"message": "mera September PF kaise bana",
                              "conversation_id": conv}, headers=rahul)

    model = FakeChatModel(say("Sure."))
    restarted = client_with(model)  # a new app = API restarted, memory empty
    rahul = auth(restarted, "rahul.verma@example.com", "Employee@123")
    sessions = restarted.get("/chat/sessions", headers=rahul).json()
    assert sessions[0]["id"] == conv and sessions[0]["title"] == "hi there"
    assert sessions[0]["messages"] == 4
    saved = restarted.get(f"/chat/sessions/{conv}", headers=rahul).json()
    assert saved["items"][3]["messages"][1]["type"] == "explain_card"  # cards are kept
    restarted.post("/chat", json={"message": "thanks", "conversation_id": conv}, headers=rahul)
    history = model.calls[0]["history"]
    assert {"role": "user", "content": "hi there"} in history  # memory rebuilt from the DB

    priya = auth(restarted, "priya.nair@example.com", "Employee@123")
    assert restarted.get(f"/chat/sessions/{conv}", headers=priya).status_code == 404
    assert restarted.get("/chat/sessions", headers=priya).json() == []
    assert restarted.delete(f"/chat/{conv}", headers=rahul).json()["cleared"] is True
    assert restarted.get("/chat/sessions", headers=rahul).json() == []
