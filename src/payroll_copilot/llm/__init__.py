"""LangChain chat models. Switch with LLM_PROVIDER = ollama | anthropic | none.

``name`` is set to ``provider:model`` (shown in chat replies and the audit log).
"""

from langchain_core.language_models import BaseChatModel

from payroll_copilot.core.config import LLMProvider, Settings, get_settings
from payroll_copilot.core.exceptions import ConfigurationError

CLAUDE_MAX_TOKENS = 16000


def get_chat_model(settings: Settings | None = None) -> BaseChatModel | None:
    settings = settings or get_settings()
    match settings.llm_provider:
        case LLMProvider.NONE:
            return None
        case LLMProvider.OLLAMA:
            from langchain_ollama import ChatOllama

            return ChatOllama(model=settings.ollama_model, base_url=settings.ollama_base_url,
                              temperature=settings.ollama_temperature,
                              client_kwargs={"timeout": 180.0},
                              name=f"ollama:{settings.ollama_model}")  # fmt: skip
        case LLMProvider.ANTHROPIC:
            if settings.anthropic_api_key is None:
                raise ConfigurationError("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY in .env")
            from langchain_anthropic import ChatAnthropic

            # no temperature: current Claude models reject non-default sampling values
            return ChatAnthropic(model=settings.anthropic_model,
                                 api_key=settings.anthropic_api_key.get_secret_value(),
                                 max_tokens=CLAUDE_MAX_TOKENS, timeout=120.0, max_retries=2,
                                 output_config={"effort": "medium"},  # chat + simple tool choice
                                 name=f"anthropic:{settings.anthropic_model}")  # fmt: skip
