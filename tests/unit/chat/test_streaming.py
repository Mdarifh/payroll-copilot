"""Streaming chat: progress events, tokens, and never an invented number on screen."""

import json

import pytest
from fastapi.testclient import TestClient

from payroll_copilot.api.main import create_app
from payroll_copilot.chat.agent import ChatAgent, GroundedStream
from tests.unit.chat.conftest import FakeChatModel, call, say


def feed_all(stream: GroundedStream, pieces: list[str]) -> str:
    return "".join(stream.feed(p) for p in pieces) + stream.flush()


@pytest.mark.parametrize(("pieces", "shown"), [
    (["Net pay ₹5", "8,0", "00 for 2026", "-08."], "Net pay ₹58,000 for 2026-08."),
    (["Net pay ₹61,", "500 after a bonus"], "Net pay ₹"),  # invented: stops before it
    (["Hello ", "Rahul"], "Hello Rahul"),
    (["LOP 1.5 days, net 58000.00"], "LOP 1.5 days, net 58000.00"),
])  # fmt: skip
def test_grounded_stream(pieces, shown):
    assert feed_all(GroundedStream('{"net_pay": "58000", "lop_days": "1.5"} 2026-08'),
                    pieces) == shown  # fmt: skip


def test_grounded_stream_learns_tool_facts_and_resets():
    stream = GroundedStream("")
    assert feed_all(stream, ["Tax ₹12,250"]) == "Tax ₹"
    stream.reset()
    stream.learn('{"tax": "12250"}')
    assert feed_all(stream, ["Tax ₹12,250."]) == "Tax ₹12,250."


def events(agent: ChatAgent, ctx, message: str) -> list[dict]:
    return list(agent.stream(ctx, message, "c"))


def test_stream_shows_progress_then_the_answer(make_ctx):
    agent = ChatAgent(FakeChatModel(call("get_payslip", month="2026-08"),
                                    say("Your August net pay was ₹58,000.")))
    out = events(agent, make_ctx(), "Help me with August")
    statuses = [e["text"] for e in out if e["event"] == "status"]
    assert statuses == ["Thinking…", "Looking up the payslip…", "Thinking…"]
    tokens = "".join(e["text"] for e in out if e["event"] == "token")
    assert tokens == "Your August net pay was ₹58,000."
    done = out[-1]
    assert done["event"] == "done" and done["reply"].messages[0]["text"] == tokens
    assert done["reply"].messages[1]["type"] == "payslip_card"


def test_stream_never_shows_an_invented_number(make_ctx):
    invented = say("Your net pay was ₹61,500 after a bonus of ₹3,500.")
    agent = ChatAgent(FakeChatModel(call("get_payslip", month="2026-08"), invented, invented))
    out = events(agent, make_ctx(), "August?")
    shown = "".join(e["text"] for e in out if e["event"] == "token")
    assert "61" not in shown and "3,500" not in shown
    assert {"event": "status", "text": "Double-checking the figures…"} in out
    assert "net pay: 58000" in out[-1]["reply"].messages[0]["text"]


def test_routed_answers_stream_as_done_only(make_ctx):
    out = events(ChatAgent(FakeChatModel()), make_ctx("HR-001"), "Rahul ka September PF kitna hai?")
    assert [e["event"] for e in out] == ["done"]
    assert out[0]["reply"].model == "intent-router"


def sse_events(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ")]  # fmt: skip


def client_for(seeded_engine, make_settings, model):
    client = TestClient(create_app(make_settings(), engine=seeded_engine, chat_model=model,
                                   worker=None))  # fmt: skip
    token = client.post("/auth/login", json={"email": "rahul.verma@example.com",
                                             "password": "Employee@123"}).json()["access_token"]
    return client, {"Authorization": f"Bearer {token}"}


def test_stream_endpoint_sends_events_and_saves_the_turn(seeded_engine, make_settings):
    client, headers = client_for(seeded_engine, make_settings, FakeChatModel(
        call("get_leave_balance"), say("Here is your leave balance.")))
    response = client.post("/chat/stream", json={"message": "leave?"}, headers=headers)
    assert response.headers["content-type"].startswith("text/event-stream")
    out = sse_events(response)
    assert out[0] == {"event": "status", "text": "Thinking…"}
    done = out[-1]
    assert done["event"] == "done" and done["tools_used"] == ["get_leave_balance"]
    assert done["messages"][0]["text"] == "Here is your leave balance."
    saved = client.get(f"/chat/sessions/{done['conversation_id']}", headers=headers).json()
    assert saved["items"][-1]["messages"][0]["text"] == "Here is your leave balance."


def test_stream_endpoint_reports_a_down_model(seeded_engine, make_settings):
    class Down(FakeChatModel):
        def _stream(self, *args, **kwargs):
            raise ConnectionError("Failed to connect to Ollama")

        _generate = _stream

    client, headers = client_for(seeded_engine, make_settings, Down())
    out = sse_events(client.post("/chat/stream", json={"message": "hi"}, headers=headers))
    assert out[-1]["event"] == "error" and out[-1]["status"] == 503
    assert "not reachable" in out[-1]["detail"]
    assert client.post("/chat/stream", json={"message": "hi"}).status_code == 401
