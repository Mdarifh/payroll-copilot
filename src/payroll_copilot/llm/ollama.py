"""Ollama (free, local) chat with tool calling over its HTTP API (/api/chat)."""

import json
from typing import Any
from uuid import uuid4

import httpx

from payroll_copilot.llm.base import (
    LLMUnavailableError,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
)


class OllamaChatModel:
    def __init__(self, model: str, base_url: str, temperature: float = 0.0,
                 http: httpx.Client | None = None, timeout: float = 180.0) -> None:  # fmt: skip
        self.model = model
        self.name = f"ollama:{model}"
        self.temperature = temperature
        self.http = http or httpx.Client(base_url=base_url, timeout=timeout)

    def complete(
        self, system: str, history: list[Any], tools: list[ToolDefinition]
    ) -> ModelTurn:
        payload = {
            "model": self.model, "stream": False,
            "messages": [{"role": "system", "content": system}, *history],
            "tools": [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters,
            }} for t in tools],
            "options": {"temperature": self.temperature},
        }  # fmt: skip
        try:
            response = self.http.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                "Ollama is not reachable - start it (ollama serve) and pull the model "
                f"(ollama pull {self.model})") from exc  # fmt: skip
        if response.status_code == 404:
            raise LLMUnavailableError(f"Ollama model {self.model} not found - run "
                                      f"`ollama pull {self.model}`")  # fmt: skip
        if response.status_code >= 400:
            raise LLMUnavailableError(f"Ollama error {response.status_code}: {response.text[:200]}")
        message = response.json().get("message", {})
        calls = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):  # some models return the arguments as a JSON string
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_invalid_json": args}
            calls.append(ToolCall(id=raw.get("id") or uuid4().hex[:12], name=fn.get("name", ""),
                                  arguments=args if isinstance(args, dict) else {}))  # fmt: skip
        stop = "tool_use" if calls else "end"
        if response.json().get("done_reason") == "length":
            stop = "max_tokens"
        return ModelTurn(text=message.get("content") or "", tool_calls=calls, stop=stop,
                         raw=message)  # fmt: skip

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def assistant_message(self, turn: ModelTurn) -> dict[str, Any]:
        return turn.raw or {"role": "assistant", "content": turn.text}

    def tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[dict[str, Any]]:
        return [{"role": "tool", "tool_name": o.call.name,
                 "content": ("ERROR: " if o.is_error else "") + o.content}
                for o in outcomes]  # fmt: skip
