import json
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from sqlalchemy import select

from payroll_copilot.chat.tools import ToolContext
from payroll_copilot.db.models import Employee

TODAY = date(2026, 10, 3)

Step = AIMessage | Callable[[list[BaseMessage]], AIMessage]


class FakeChatModel(BaseChatModel):
    """Scripted LangChain chat model: each step is an AIMessage or a
    function(history) -> AIMessage. Records the system prompt, history and bound tools."""

    name: str | None = "fake:scripted"
    steps: list[Any] = []
    calls: list[dict[str, Any]] = []

    def __init__(self, *steps: Step) -> None:
        super().__init__(steps=list(steps), calls=[])

    @property
    def _llm_type(self) -> str:
        return "fake-scripted"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tool_names=[t.name for t in tools])

    def _next(self, messages, kwargs) -> AIMessage:
        system, history = messages[0], list(messages[1:])
        self.calls.append({"system": system.content, "history": history,
                           "tools": kwargs.get("tool_names", [])})  # fmt: skip
        step = self.steps.pop(0) if self.steps else say("(no more steps)")
        message = step(history) if callable(step) else step
        # a fresh copy each time: the same scripted step may be returned twice
        return message.model_copy(update={"id": None}, deep=True)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages, kwargs))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """Streaming (used when the chat graph streams): text in 4-character pieces."""
        message = self._next(messages, kwargs)
        if message.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[
                {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
                for i, c in enumerate(message.tool_calls)]))
            return
        text = message.text
        for start in range(0, len(text), 4):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=text[start:start + 4]))
            if run_manager:
                run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk
        yield ChatGenerationChunk(message=AIMessageChunk(
            content="", response_metadata=message.response_metadata))


def call(name: str, **arguments: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments,
                                              "id": f"c-{name}"}])  # fmt: skip


def say(text: str) -> AIMessage:
    return AIMessage(content=text)


@pytest.fixture
def make_ctx(seeded_session):
    def _make(code: str = "EMP-101") -> ToolContext:
        user = seeded_session.scalar(select(Employee).where(Employee.employee_code == code))
        return ToolContext(session=seeded_session, user=user, today=TODAY, request_id="req-test")

    return _make
