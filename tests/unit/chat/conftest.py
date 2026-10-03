from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from sqlalchemy import select

from payroll_copilot.chat.tools import ToolContext
from payroll_copilot.db.models import Employee
from payroll_copilot.llm.base import ModelTurn, ToolCall, ToolDefinition, ToolOutcome

TODAY = date(2026, 10, 3)


class FakeChatModel:
    """Scripted model: each step is a ModelTurn or a function(history) -> ModelTurn."""

    name = "fake:scripted"

    def __init__(self, *steps: ModelTurn | Callable[[list[Any]], ModelTurn]) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    def complete(self, system: str, history: list[Any], tools: list[ToolDefinition]) -> ModelTurn:
        self.calls.append({"system": system, "history": list(history),
                           "tools": [t.name for t in tools]})  # fmt: skip
        step = self.steps.pop(0) if self.steps else ModelTurn(text="(no more steps)")
        return step(history) if callable(step) else step

    def user_message(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def assistant_message(self, turn: ModelTurn) -> dict[str, Any]:
        return {"role": "assistant", "content": turn.text,
                "tool_calls": [c.name for c in turn.tool_calls]}  # fmt: skip

    def tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[dict[str, Any]]:
        return [{"role": "tool", "name": o.call.name, "content": o.content,
                 "is_error": o.is_error} for o in outcomes]  # fmt: skip


def call(name: str, **arguments: Any) -> ModelTurn:
    return ModelTurn(text="", tool_calls=[ToolCall(id=f"c-{name}", name=name,
                                                   arguments=arguments)], stop="tool_use")


def say(text: str) -> ModelTurn:
    return ModelTurn(text=text)


@pytest.fixture
def make_ctx(seeded_session):
    def _make(code: str = "EMP-101") -> ToolContext:
        user = seeded_session.scalar(select(Employee).where(Employee.employee_code == code))
        return ToolContext(session=seeded_session, user=user, today=TODAY, request_id="req-test")

    return _make
