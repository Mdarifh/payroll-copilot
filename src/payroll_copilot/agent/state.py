"""Graph state. Per-turn fields are reset by the first node; memory fields persist."""

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ToolRun(TypedDict):
    name: str
    args: dict[str, Any]
    content: str
    artifact: dict[str, Any] | None
    ok: bool


class AgentState(TypedDict, total=False):
    # conversation (persists per thread)
    messages: Annotated[list[AnyMessage], add_messages]
    last_employee_code: str | None
    last_period: str | None
    # per turn
    question: str
    intent: str
    matched: str | None
    entities: dict[str, Any]
    route: str
    missing: list[str]
    needs_rules: bool
    module: str | None
    employee: dict[str, Any] | None
    tool_runs: list[ToolRun]
    policy: dict[str, Any] | None
    checks: list[str]
    explanation: str
    used_llm: bool
    answer: str
    warnings: list[str]
    sources: list[str]
    pending_action: dict[str, Any] | None  # write action waiting for the user's yes/no
