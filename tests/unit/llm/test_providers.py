"""LangChain chat models: factory, request shape, error mapping (no network)."""

import json

import anthropic
import httpx
import httpx2
import ollama
import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langchain_ollama import ChatOllama

from payroll_copilot.core.config import LLMProvider
from payroll_copilot.core.exceptions import ConfigurationError
from payroll_copilot.llm import get_chat_model
from payroll_copilot.llm.base import unavailable

PAYSLIP = StructuredTool.from_function(
    func=lambda **_: "", name="get_payslip", description="Payslip",
    args_schema={"type": "object", "properties": {"month": {"type": "string"}}})  # fmt: skip


# ---------------------------------------------------------------------- factory


def test_factory(make_settings):
    assert get_chat_model(make_settings(llm_provider="none")) is None
    local = get_chat_model(make_settings())
    assert isinstance(local, ChatOllama) and local.name == "ollama:qwen2.5:7b"
    assert local.temperature == 0.0
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        get_chat_model(make_settings(llm_provider=LLMProvider.ANTHROPIC))
    claude = get_chat_model(make_settings(llm_provider="anthropic", anthropic_api_key="sk-x"))
    assert isinstance(claude, ChatAnthropic) and claude.name == "anthropic:claude-sonnet-5"


def test_claude_request_has_no_sampling_parameters(make_settings):
    claude = get_chat_model(make_settings(llm_provider="anthropic", anthropic_api_key="sk-x"))
    payload = claude._get_request_payload([HumanMessage("hi")])
    assert payload["model"] == "claude-sonnet-5" and payload["max_tokens"] == 16000
    assert payload["output_config"] == {"effort": "medium"}
    assert "temperature" not in payload  # rejected by current models


# ---------------------------------------------------------------------- Ollama


def test_ollama_request_and_tool_call():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=json.dumps({
            "model": "qwen2.5:7b", "created_at": "2026-10-04T00:00:00Z", "done": True,
            "done_reason": "stop", "message": {"role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "get_payslip",
                                             "arguments": {"month": "2026-08"}}}]}}) + "\n")  # fmt: skip

    model = ChatOllama(model="qwen2.5:7b", base_url="http://ollama", temperature=0.0,
                       client_kwargs={"transport": httpx.MockTransport(handler)})  # fmt: skip
    reply = model.bind_tools([PAYSLIP]).invoke([HumanMessage("hi")])
    assert seen["model"] == "qwen2.5:7b" and seen["options"] == {"temperature": 0.0}
    assert seen["tools"][0]["function"]["name"] == "get_payslip"
    assert [(c["name"], c["args"]) for c in reply.tool_calls] == [("get_payslip",
                                                                   {"month": "2026-08"})]


# ---------------------------------------------------------------------- errors


def _status_error(cls, status: int):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("err", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(("error", "text"), [
    (ConnectionError("Failed to connect to Ollama"), "Ollama is not reachable"),
    (httpx.ConnectError("refused"), "Ollama is not reachable"),
    (ollama.ResponseError("model not found", 404), "ollama pull qwen2.5:7b"),
    (ollama.ResponseError("boom", 500), "Ollama error 500"),
    (_status_error(anthropic.AuthenticationError, 401), "API key is invalid"),
    (_status_error(anthropic.RateLimitError, 429), "rate limit"),
    (_status_error(anthropic.InternalServerError, 500), "Claude API error 500"),
    (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), "not reachable"),
])  # fmt: skip
def test_provider_errors_become_readable(error, text):
    assert text in str(unavailable(error, "ollama:qwen2.5:7b"))


def test_other_errors_are_not_hidden():
    assert unavailable(ValueError("bug"), "ollama:m") is None
