"""Errors of the LangChain chat models, translated into one message the API can show.

The models themselves are LangChain ``BaseChatModel`` objects (``ChatOllama`` or
``ChatAnthropic``, see ``payroll_copilot.llm``); the chat graph only calls ``invoke``.
"""

import anthropic
import httpx
import ollama

from payroll_copilot.core.exceptions import PayrollCopilotError


class LLMUnavailableError(PayrollCopilotError):
    """The model could not be reached or refused the credentials (HTTP 503)."""


def unavailable(exc: Exception, model: str) -> LLMUnavailableError | None:
    """A readable LLMUnavailableError for a provider error; None for anything else.

    ``model`` is the provider-prefixed name, e.g. ``ollama:qwen2.5:3b``.
    """
    name = model.split(":", 1)[-1]
    match exc:
        case ollama.ResponseError() if exc.status_code == 404:
            return LLMUnavailableError(f"Ollama model {name} not found - run `ollama pull {name}`")
        case ollama.ResponseError():
            return LLMUnavailableError(f"Ollama error {exc.status_code}: {str(exc.error)[:200]}")
        case ConnectionError() | httpx.HTTPError():
            return LLMUnavailableError("Ollama is not reachable - start it (ollama serve) and "
                                       f"pull the model (ollama pull {name})")  # fmt: skip
        case anthropic.AuthenticationError():
            return LLMUnavailableError("Claude API key is invalid (ANTHROPIC_API_KEY)")
        case anthropic.PermissionDeniedError():
            return LLMUnavailableError("Claude API key lacks permission for this model")
        case anthropic.NotFoundError():
            return LLMUnavailableError(f"Claude model {name} not found")
        case anthropic.RateLimitError():
            return LLMUnavailableError("Claude API rate limit reached - try again shortly")
        case anthropic.APIStatusError():
            return LLMUnavailableError(f"Claude API error {exc.status_code}")
        case anthropic.APIConnectionError():
            return LLMUnavailableError("Claude API not reachable (network)")
    return None
