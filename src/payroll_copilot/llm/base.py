"""Provider-neutral view of one model call, so the chat loop works with any provider.

Each provider keeps the conversation in its own native message format (the Claude
API needs its content blocks - including thinking blocks - sent back unchanged);
the loop only sees ``ModelTurn`` and asks the provider to build messages.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from payroll_copilot.core.exceptions import PayrollCopilotError


class LLMUnavailableError(PayrollCopilotError):
    """The model could not be reached or refused the credentials (HTTP 503)."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    call: ToolCall
    content: str  # JSON text the model reads
    is_error: bool = False


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop: str = "end"  # end | tool_use | refusal | max_tokens
    raw: Any = None  # provider's assistant message, appended to history as-is


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


class ChatModel(Protocol):
    name: str  # e.g. "ollama:qwen2.5:7b"

    def complete(
        self, system: str, history: list[Any], tools: list[ToolDefinition]
    ) -> ModelTurn: ...

    def user_message(self, text: str) -> Any: ...

    def assistant_message(self, turn: ModelTurn) -> Any: ...

    def tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[Any]: ...
