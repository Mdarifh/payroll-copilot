"""Chat model factory. Default is a free local model through Ollama.

Creating the model does not contact the server; ``check_llm`` does a cheap
reachability check so the CLI/UI can explain what is missing.
"""

from dataclasses import dataclass

import httpx
from langchain_core.language_models import BaseChatModel

from payroll_copilot.core.config import LLMProvider, Settings, get_settings
from payroll_copilot.core.exceptions import ConfigurationError


def get_chat_model(settings: Settings | None = None) -> BaseChatModel | None:
    """The configured chat model, or None when LLM_PROVIDER=none."""
    settings = settings or get_settings()
    provider = settings.llm_provider
    if provider is LLMProvider.NONE:
        return None
    issues = settings.config_issues()
    if issues:
        raise ConfigurationError("; ".join(issues))
    if provider is LLMProvider.OLLAMA:
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.llm_model,
            base_url=settings.ollama_base_url,
            temperature=settings.llm_temperature,
        )
    raise ConfigurationError(
        f"LLM_PROVIDER={provider.value} is not wired in this build; use 'ollama' (free, local) "
        "or 'none'"
    )


@dataclass(frozen=True)
class LLMStatus:
    provider: str
    model: str | None
    ready: bool
    message: str


def check_llm(settings: Settings | None = None, timeout: float = 3.0) -> LLMStatus:
    settings = settings or get_settings()
    provider, model = settings.llm_provider, settings.llm_model
    if provider is LLMProvider.NONE:
        return LLMStatus("none", None, False, "LLM disabled (LLM_PROVIDER=none)")
    if provider is not LLMProvider.OLLAMA:
        return LLMStatus(provider.value, model, False, "provider not wired in this build")
    try:
        response = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        message = (
            f"Ollama server not reachable at {settings.ollama_base_url} "
            f"({exc.__class__.__name__}); start the Ollama app or run `ollama serve`"
        )
        return LLMStatus("ollama", model, False, message)
    names = [m.get("name", "") for m in response.json().get("models", [])]
    if model not in names and f"{model}:latest" not in names:
        return LLMStatus("ollama", model, False,
                         f"model {model} not pulled; run `ollama pull {model}` "
                         f"(available: {names or 'none'})")  # fmt: skip
    return LLMStatus("ollama", model, True, f"Ollama ready with {model}")
