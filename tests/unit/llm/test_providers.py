"""Provider adapters: request shape, response parsing, error mapping (no network)."""

import json

import anthropic
import httpx
import httpx2
import pytest
from anthropic.types import Message

from payroll_copilot.core.config import LLMProvider
from payroll_copilot.core.exceptions import ConfigurationError
from payroll_copilot.llm import get_chat_model
from payroll_copilot.llm.base import LLMUnavailableError, ToolCall, ToolDefinition, ToolOutcome
from payroll_copilot.llm.claude import ClaudeChatModel
from payroll_copilot.llm.ollama import OllamaChatModel

TOOLS = [ToolDefinition("get_payslip", "Payslip", {"type": "object", "properties": {}})]


# ---------------------------------------------------------------------- Ollama


def ollama(handler) -> OllamaChatModel:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ollama")
    return OllamaChatModel("qwen2.5:7b", "http://ollama", http=http)


def test_ollama_request_and_tool_call():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"done_reason": "stop", "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": "get_payslip",
                                         "arguments": {"month": "2026-08"}}}]}})  # fmt: skip

    turn = ollama(handler).complete("SYSTEM", [{"role": "user", "content": "hi"}], TOOLS)
    assert seen["model"] == "qwen2.5:7b" and seen["stream"] is False
    assert seen["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert seen["tools"][0]["function"]["name"] == "get_payslip"
    assert seen["options"] == {"temperature": 0.0}
    assert turn.stop == "tool_use"
    assert (turn.tool_calls[0].name, turn.tool_calls[0].arguments) == ("get_payslip",
                                                                       {"month": "2026-08"})


def test_ollama_string_arguments_and_text():
    def handler(_):
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "ok",
            "tool_calls": [{"function": {"name": "x", "arguments": '{"a": 1}'}},
                           {"function": {"name": "y", "arguments": "not json"}}]}})  # fmt: skip

    turn = ollama(handler).complete("s", [], TOOLS)
    assert [c.arguments for c in turn.tool_calls] == [{"a": 1}, {"_invalid_json": "not json"}]


@pytest.mark.parametrize(("handler", "message"), [
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), "not reachable"),
    (lambda r: httpx.Response(404, json={"error": "model not found"}), "ollama pull qwen2.5:7b"),
    (lambda r: httpx.Response(500, text="boom"), "Ollama error 500"),
])  # fmt: skip
def test_ollama_errors(handler, message):
    with pytest.raises(LLMUnavailableError, match=message):
        ollama(handler).complete("s", [], TOOLS)


def test_ollama_tool_results():
    model = OllamaChatModel("m", "http://x")
    msgs = model.tool_result_messages([ToolOutcome(ToolCall("1", "get_payslip", {}), '{"a":1}'),
                                       ToolOutcome(ToolCall("2", "x", {}), "bad", True)])
    assert msgs == [{"role": "tool", "tool_name": "get_payslip", "content": '{"a":1}'},
                    {"role": "tool", "tool_name": "x", "content": "ERROR: bad"}]  # fmt: skip


# ---------------------------------------------------------------------- Claude


def message(content: list[dict], stop_reason: str) -> Message:
    return Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })  # fmt: skip


class FakeMessages:
    def __init__(self, result):
        self.result, self.kwargs = result, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result):
        self.messages = FakeMessages(result)


def test_claude_request_and_tool_use():
    reply = message([{"type": "text", "text": "Let me check."},
                     {"type": "tool_use", "id": "toolu_1", "name": "get_payslip",
                      "input": {"month": "2026-08"}}], "tool_use")  # fmt: skip
    client = FakeClient(reply)
    model = ClaudeChatModel("claude-sonnet-5", client=client)
    turn = model.complete("SYSTEM", [{"role": "user", "content": "hi"}], TOOLS)
    kwargs = client.messages.kwargs
    assert kwargs["model"] == "claude-sonnet-5" and kwargs["system"] == "SYSTEM"
    assert kwargs["tools"][0] == {"name": "get_payslip", "description": "Payslip",
                                  "input_schema": {"type": "object", "properties": {}}}
    assert "temperature" not in kwargs  # rejected by current models
    assert turn.stop == "tool_use" and turn.text == "Let me check."
    assert turn.tool_calls == [ToolCall("toolu_1", "get_payslip", {"month": "2026-08"})]
    # the full content (all blocks) goes back unchanged
    assert model.assistant_message(turn) == {"role": "assistant", "content": reply.content}


def test_claude_tool_results_in_one_user_message():
    model = ClaudeChatModel("m", client=FakeClient(None))
    msgs = model.tool_result_messages([ToolOutcome(ToolCall("t1", "a", {}), "{}"),
                                       ToolOutcome(ToolCall("t2", "b", {}), "nope", True)])
    assert msgs == [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "{}", "is_error": False},
        {"type": "tool_result", "tool_use_id": "t2", "content": "nope", "is_error": True}]}]


def test_claude_refusal_drops_tool_calls():
    model = ClaudeChatModel("m", client=FakeClient(message([], "refusal")))
    assert model.complete("s", [], TOOLS).stop == "refusal"


def _status_error(cls, status: int):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("err", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(("error", "text"), [
    (_status_error(anthropic.AuthenticationError, 401), "API key is invalid"),
    (_status_error(anthropic.RateLimitError, 429), "rate limit"),
    (_status_error(anthropic.InternalServerError, 500), "Claude API error 500"),
    (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), "not reachable"),
])  # fmt: skip
def test_claude_errors(error, text):
    with pytest.raises(LLMUnavailableError, match=text):
        ClaudeChatModel("m", client=FakeClient(error)).complete("s", [], TOOLS)


# ---------------------------------------------------------------------- factory


def test_factory(make_settings):
    assert get_chat_model(make_settings(llm_provider="none")) is None
    assert get_chat_model(make_settings()).name == "ollama:qwen2.5:7b"
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        get_chat_model(make_settings(llm_provider=LLMProvider.ANTHROPIC))
    claude = get_chat_model(make_settings(llm_provider="anthropic", anthropic_api_key="sk-x"))
    assert claude.name == "anthropic:claude-sonnet-5"
