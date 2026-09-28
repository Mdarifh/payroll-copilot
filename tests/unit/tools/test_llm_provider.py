import httpx
import pytest

from payroll_copilot.core.exceptions import ConfigurationError
from payroll_copilot.llm.provider import check_llm, get_chat_model
from payroll_copilot.tools import build_tools


def test_no_llm(make_settings):
    assert get_chat_model(make_settings()) is None
    status = check_llm(make_settings())
    assert not status.ready and "disabled" in status.message


def test_ollama_model_is_built_without_network(make_settings):
    llm = get_chat_model(make_settings(llm_provider="ollama", llm_model="qwen2.5:7b"))
    assert llm.model == "qwen2.5:7b" and llm.temperature == 0


def test_tools_bind_to_ollama_offline(make_settings):
    llm = get_chat_model(make_settings(llm_provider="ollama", llm_model="qwen2.5:7b"))
    bound = llm.bind_tools(build_tools())
    names = {t["function"]["name"] for t in bound.kwargs["tools"]}
    assert {"calculate_pf", "search_payroll_policy", "compare_expected_actual"} <= names


@pytest.mark.parametrize(("overrides", "message"), [
    ({"llm_provider": "ollama"}, "LLM_MODEL is not set"),
    ({"llm_provider": "anthropic", "llm_model": "m", "anthropic_api_key": "k"}, "not wired"),
])  # fmt: skip
def test_misconfigured(make_settings, overrides, message):
    with pytest.raises(ConfigurationError, match=message):
        get_chat_model(make_settings(**overrides))


class FakeResponse:
    def __init__(self, models):
        self._models = models

    def raise_for_status(self):
        return None

    def json(self):
        return {"models": [{"name": n} for n in self._models]}


@pytest.mark.parametrize(("models", "ready", "needle"), [
    (["qwen2.5:7b"], True, "Ollama ready"),
    (["llama3.2:latest"], False, "ollama pull qwen2.5:7b"),
])  # fmt: skip
def test_check_llm_with_server(make_settings, monkeypatch, models, ready, needle):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse(models))
    status = check_llm(make_settings(llm_provider="ollama", llm_model="qwen2.5:7b"))
    assert status.ready is ready and needle in status.message


def test_check_llm_server_down(make_settings, monkeypatch):
    def refuse(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", refuse)
    status = check_llm(make_settings(llm_provider="ollama", llm_model="qwen2.5:7b"))
    assert not status.ready and "ollama serve" in status.message
