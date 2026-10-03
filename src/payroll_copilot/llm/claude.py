"""Claude (Anthropic API) chat with tool calling - a manual tool-use loop step.

Uses the official ``anthropic`` SDK. The assistant's full ``content`` (text, tool_use
and thinking blocks) is sent back unchanged on the next request, as the API requires.
Sampling parameters are not sent: current Claude models reject non-default values.
"""

from typing import Any

import anthropic

from payroll_copilot.llm.base import (
    LLMUnavailableError,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
)

MAX_TOKENS = 16000


class ClaudeChatModel:
    def __init__(self, model: str, api_key: str | None = None,
                 client: anthropic.Anthropic | None = None) -> None:  # fmt: skip
        self.model = model
        self.name = f"anthropic:{model}"
        self.client = client or anthropic.Anthropic(api_key=api_key, timeout=120.0, max_retries=2)

    def complete(
        self, system: str, history: list[Any], tools: list[ToolDefinition]
    ) -> ModelTurn:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=[{"name": t.name, "description": t.description,
                        "input_schema": t.parameters} for t in tools],  # fmt: skip
                output_config={"effort": "medium"},  # chat + simple tool choice
                messages=history,
            )
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailableError("Claude API key is invalid (ANTHROPIC_API_KEY)") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMUnavailableError("Claude API key lacks permission for this model") from exc
        except anthropic.NotFoundError as exc:
            raise LLMUnavailableError(f"Claude model {self.model} not found") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailableError("Claude API rate limit reached - try again shortly") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailableError(f"Claude API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError("Claude API not reachable (network)") from exc

        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [ToolCall(id=b.id, name=b.name, arguments=dict(b.input or {}))
                 for b in response.content if b.type == "tool_use"]  # fmt: skip
        stop = {"tool_use": "tool_use", "refusal": "refusal",
                "max_tokens": "max_tokens"}.get(response.stop_reason or "", "end")  # fmt: skip
        return ModelTurn(text=text, tool_calls=calls if stop == "tool_use" else [], stop=stop,
                         raw={"role": "assistant", "content": response.content})  # fmt: skip

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def assistant_message(self, turn: ModelTurn) -> dict[str, Any]:
        return turn.raw or {"role": "assistant", "content": turn.text}

    def tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[dict[str, Any]]:
        # all results of one assistant turn go back together in a single user message
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": o.call.id, "content": o.content,
             "is_error": o.is_error} for o in outcomes]}]  # fmt: skip
