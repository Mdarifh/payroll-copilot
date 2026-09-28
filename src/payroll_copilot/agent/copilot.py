"""Facade: build the graph with real tools and ask questions per conversation thread.

Conversation memory is persisted with LangGraph's SQLite checkpointer
(``CHECKPOINT_DB``), so a thread keeps its context across restarts. Every turn is
written to the audit log with intent, route, tools and rule versions used.
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from sqlalchemy import select

from payroll_copilot.agent.actions import NO, YES, reply_word
from payroll_copilot.agent.graph import build_graph
from payroll_copilot.audit.context import audit_context, current_actor
from payroll_copilot.audit.logger import rule_versions
from payroll_copilot.core.config import get_settings
from payroll_copilot.db.models import Employee, PTRule
from payroll_copilot.llm.provider import get_chat_model
from payroll_copilot.tools import ToolContext, build_tools


@dataclass
class CopilotReply:
    answer: str
    intent: str
    route: str
    entities: dict[str, Any]
    tools_used: list[str]
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    used_llm: bool = False
    request_id: str | None = None
    pending_action: dict[str, Any] | None = None  # waiting for yes / no


class ApprovalActions:
    """ActionHandler backed by the approval service (previews read, submit writes)."""

    def __init__(self, context: ToolContext) -> None:
        self.context = context

    def preview(self, action, payload):
        from payroll_copilot.services.approval_service import (
            PAYLOADS,
            ApprovalError,
            ApprovalService,
        )

        try:
            data = PAYLOADS[action].model_validate(payload).model_dump(mode="json")
        except ValueError as exc:
            raise ApprovalError(f"invalid request: {exc}") from exc
        with self.context.sessions() as s:
            return ApprovalService(s)._preview(action, data)

    def submit(self, action, payload, actor):
        from payroll_copilot.services.approval_service import ApprovalService

        with self.context.write_sessions() as s:
            view = ApprovalService(s).request(action, payload, requested_by=actor)
        return view.model_dump()


def sqlite_checkpointer(path: Path | None = None) -> SqliteSaver:
    path = path or get_settings().checkpoint_db
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


class PayrollCopilot:
    def __init__(
        self,
        context: ToolContext | None = None,
        llm: BaseChatModel | None = None,
        *,
        use_configured_llm: bool = True,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self.context = context or ToolContext()
        if llm is None and use_configured_llm:
            llm = get_chat_model()
        self.llm = llm
        self.checkpointer = checkpointer or sqlite_checkpointer()
        self.graph = build_graph(
            build_tools(self.context), llm,
            directory=self._directory, known_states=self._states(),
            checkpointer=self.checkpointer, actions=ApprovalActions(self.context),
        )  # fmt: skip

    def _directory(self) -> list[tuple[str, str]]:
        with self.context.sessions() as s:
            return [(e.employee_code, e.full_name) for e in s.scalars(select(Employee))]

    def _states(self) -> list[str]:
        with self.context.sessions() as s:
            return list(s.scalars(select(PTRule.state_code).distinct()))

    @staticmethod
    def _config(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    def ask(
        self, question: str, thread_id: str = "default", actor: str | None = None
    ) -> CopilotReply:
        with audit_context(actor=actor) as request_id:
            config = self._config(thread_id)
            waiting = "submit_action" in (self.graph.get_state(config).next or ())
            if waiting:
                word = reply_word(question)
                if word not in YES | NO:
                    pending = self.graph.get_state(config).values.get("pending_action") or {}
                    return CopilotReply(
                        answer=("Ek action aapke jawab ka intezaar kar raha hai: "
                                f"{pending.get('description', '')}. 'yes' likhiye submit karne ke "
                                "liye, ya 'no' cancel karne ke liye."),
                        intent="ACTION_PENDING", route="action_pending", entities={},
                        tools_used=[], request_id=request_id, pending_action=pending,
                    )  # fmt: skip
                state = self.graph.invoke(
                    Command(resume={"confirm": word in YES, "actor": current_actor()}),
                    config=config,
                )
            else:
                state = self.graph.invoke(
                    {"messages": [HumanMessage(content=question)]}, config=config
                )
            runs = state.get("tool_runs", [])
            reply = CopilotReply(
                answer=state["answer"],
                intent=state["intent"],
                route=state.get("route") or state["intent"],
                entities=state.get("entities", {}),
                tools_used=[r["name"] for r in runs],
                artifacts=[r["artifact"] for r in runs if r["artifact"]],
                sources=state.get("sources", []),
                warnings=state.get("warnings", []),
                checks=state.get("checks", []),
                used_llm=state.get("used_llm", False),
                request_id=request_id,
                pending_action=state.get("pending_action") if "__interrupt__" in state else None,
            )
            self.context.audit.log(
                "chat.ask", entity="thread", entity_id=thread_id,
                details={
                    "question": question, "intent": reply.intent, "route": reply.route,
                    "entities": {k: v for k, v in reply.entities.items() if v},
                    "tools": reply.tools_used, "rule_versions": rule_versions(reply.artifacts),
                    "sources": len(reply.sources), "warnings": reply.warnings[:10],
                    "used_llm": reply.used_llm, "answer_preview": reply.answer[:300],
                },
            )  # fmt: skip
            return reply

    def history(self, thread_id: str) -> list[dict[str, str]]:
        """Messages stored for a thread (oldest first)."""
        snapshot = self.graph.get_state(self._config(thread_id))
        messages = (snapshot.values or {}).get("messages", [])
        return [{"role": "user" if m.type == "human" else "assistant", "content": str(m.content)}
                for m in messages]  # fmt: skip

    def memory(self, thread_id: str) -> dict[str, Any]:
        values = self.graph.get_state(self._config(thread_id)).values or {}
        return {"last_employee_code": values.get("last_employee_code"),
                "last_period": values.get("last_period"),
                "turns": sum(1 for m in values.get("messages", []) if m.type == "human")}

    def clear(self, thread_id: str, actor: str | None = None) -> None:
        self.checkpointer.delete_thread(thread_id)
        with audit_context(actor=actor):
            self.context.audit.log("chat.clear", entity="thread", entity_id=thread_id)
