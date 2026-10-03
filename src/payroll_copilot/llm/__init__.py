"""LLM providers behind one interface. Switch with LLM_PROVIDER = ollama | anthropic | none."""

from payroll_copilot.core.config import LLMProvider, Settings, get_settings
from payroll_copilot.core.exceptions import ConfigurationError
from payroll_copilot.llm.base import ChatModel


def get_chat_model(settings: Settings | None = None) -> ChatModel | None:
    settings = settings or get_settings()
    match settings.llm_provider:
        case LLMProvider.NONE:
            return None
        case LLMProvider.OLLAMA:
            from payroll_copilot.llm.ollama import OllamaChatModel

            return OllamaChatModel(settings.ollama_model, settings.ollama_base_url,
                                   settings.ollama_temperature)  # fmt: skip
        case LLMProvider.ANTHROPIC:
            if settings.anthropic_api_key is None:
                raise ConfigurationError("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY in .env")
            from payroll_copilot.llm.claude import ClaudeChatModel

            return ClaudeChatModel(settings.anthropic_model,
                                   settings.anthropic_api_key.get_secret_value())  # fmt: skip
