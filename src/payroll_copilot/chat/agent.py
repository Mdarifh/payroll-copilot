"""The chat as a LangGraph state graph; the model is a LangChain chat model.

    START -> route --(answered by the intent router)----------------> finish -> END
               |
               +--> agent <--> tools   (at most LLM_MAX_STEPS model calls)
                      |
                      +--> ground --------------------------------> finish -> END

* route  - deterministic intent router: payroll questions it recognises are answered
           from the engine directly (exact, fast, no LLM); writes only produce a preview.
* agent  - the LLM (``ChatOllama`` / ``ChatAnthropic``) with the user's role's tools bound.
* tools  - runs the requested LangChain tools (role check, validation and audit happen in
           ``chat.tools.run_tool``); UI cards travel as tool artifacts, not through the model.
* ground - if the model wrote a number that no tool returned, it gets one retry (back to
           agent with a hint); after that its text is replaced by the tool facts (numbers
           are never invented by the LLM). A claimed change without a tool is replaced too.
           Memory keeps the text the user saw, never a rejected draft.
* finish - keeps the last MAX_TURNS_KEPT exchanges in the conversation memory.

Conversation memory is LangGraph's checkpointer (one thread per user + conversation). The
API passes a SQLite checkpointer, so the memory survives a restart; with the in-memory
default it is rebuilt from the saved chat history.
"""

import json
import re
import threading
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import Runnable
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

from payroll_copilot.auth.permissions import is_staff
from payroll_copilot.chat.tools import ToolContext, ToolError, langchain_tools, run_tool, tools_for
from payroll_copilot.core.config import get_settings
from payroll_copilot.llm.base import unavailable
from payroll_copilot.services import audit

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
TOOL_STATUS = {
    "get_payslip": "Looking up the payslip…", "get_leave_balance": "Checking leave balance…",
    "get_tax_summary": "Calculating tax…", "get_attendance_exceptions": "Checking attendance…",
    "start_payroll_review": "Running payroll pre-checks…",
    "explain_my_pay": "Reading the payroll calculation…",
    "explain_employee_pay": "Reading the payroll calculation…",
    "compare_my_months": "Comparing the months…",
    "compare_employee_months": "Comparing the months…",
    "payroll_rules": "Reading the statutory rules…", "find_employee": "Finding the employee…",
    "search_policy_documents": "Reading policy documents…",
}  # fmt: skip
_DONE = r"(updated|changed|applied|processed|revised|increased|decreased|set|marked|approved|saved)"
# the model claiming that it changed data (it has no tool that writes anything)
_CHANGE_CLAIM = re.compile(rf"\b(?:is|are|has been|have been|was|were) now\b|"
                           rf"\b(?:has|have) been {_DONE}\b|"
                           rf"\b(?:i|we)(?:'ve| have)? (?:now )?{_DONE}\b|"
                           rf"\b{_DONE} (?:it |this |that )?to\b", re.IGNORECASE)  # fmt: skip
MAX_TURNS_KEPT = 10  # user turns remembered per conversation
MAX_CONVERSATIONS_PER_USER = 20
OFF_TEXT = "The AI assistant is switched off (LLM_PROVIDER=none). Please contact HR."
REFUSAL_TEXT = "Sorry, I can't help with that request. Please contact HR."
UNRELIABLE_TEXT = ("Sorry, I could not answer that reliably. Please ask again with the "
                   "employee and month, e.g. \"Show my tax summary for 2026-27\" or "
                   "\"Rahul ka September PF kitna hai?\"")  # fmt: skip
NO_CHANGE_TEXT = ("Nothing was changed - I can't change payroll data on my own. Ask for the "
                  "change with the employee, e.g. \"Rahul ki salary 60000 kar do\", "
                  "\"EMP-101 ka 2 din LOP lagao\" or \"15 Sep absent mark karo\": you get a "
                  "preview card to review and apply. Or use the Employees page.")  # fmt: skip
RETRY_HINT = ("Your previous draft stated figures that no tool returned in this turn. Call "
              "the tool that provides the figures the user asked for and answer only from its "
              "result. If no tool fits, answer without any figures.")  # fmt: skip

SYSTEM_PROMPT = """You are the payroll assistant of {company}, an Indian company.
Today is {today}. The current financial year is {fy} (April to March).
You are talking to {name} ({code}), role: {role}.

How to answer:
- Use the tools for every payroll fact. Never calculate, estimate or invent amounts,
  days or dates - only repeat numbers that a tool returned.
- The tools always act for the logged-in user; you cannot look up another employee's
  payslip, leave or tax{hr_note}.
- Months are YYYY-MM (e.g. "last month" = {last_month}). If the user's request is
  ambiguous, ask a short question instead of guessing.
- When a payslip tool succeeds, the app shows the payslip card itself: reply with one or
  two sentences (net pay and anything unusual such as LOP days), not the full table.
- Money is in Indian rupees with Indian digit grouping, e.g. ₹1,25,000.
- LOP means Loss of Pay (unpaid days), PF = Provident Fund, PT = Professional Tax,
  TDS = income tax deducted at source.
- For policy questions (leave rules, overtime, payroll process, ...) call
  search_policy_documents and answer only from the passages it returns, naming the
  document. If it finds nothing, say you don't know yet and suggest contacting HR.
- You cannot change any data (salary, LOP, attendance, payroll): no tool writes anything.
  Never say something was updated, changed or applied. When asked for a change, say the
  user should ask with the employee and amount (e.g. "Rahul ki salary 60000 kar do") to
  get a preview card, or use the Employees page.
- Earlier answers in this conversation may be outdated: call a tool again for any figure.
- If a tool returns an error, explain it plainly.
- Always reply in English, briefly and politely - even when the user writes in Hinglish
  (Hindi in English letters, e.g. "mera August ka payslip dikhao"), which you understand."""

HR_NOTE = ("; as HR you can check any employee's attendance exceptions with "
           "get_attendance_exceptions. To process / run payroll for a month (all employees, "
           "a department or a state/office) call start_payroll_review once: it opens a card "
           "where HR reviews LOP and starts the run. Payroll is NOT processed until HR "
           "presses the run button in that card - never say it is done")  # fmt: skip


@dataclass
class ChatReply:
    conversation_id: str
    messages: list[dict[str, Any]]  # {"type": "text" | "payslip_card" | ..., ...}
    tools_used: list[str] = field(default_factory=list)
    model: str | None = None
    warnings: list[str] = field(default_factory=list)


class ChatState(TypedDict):
    """Graph state. ``messages`` is the conversation (kept by the checkpointer); the other
    keys describe the current turn and are reset by every ``ask``."""

    messages: Annotated[list[AnyMessage], add_messages]
    reply: str
    cards: list[dict[str, Any]]
    tools_used: list[str]
    facts: list[dict[str, Any]]  # {"content": tool output, "is_error": bool}
    steps: int
    stop: str  # routed | end | tool_use | refusal | max_tokens | off | retry
    retried: bool  # the model already got a second chance after ungrounded figures
    model: str | None
    warnings: list[str]


@dataclass
class TurnContext:
    """Per-request objects for the graph's nodes (LangGraph runtime context, not saved)."""

    ctx: ToolContext
    message: str
    system: str
    tools: list[StructuredTool]  # every tool, bound to this user (run_tool checks the role)
    model: Runnable | None  # the chat model with the role's tools bound


def _norm(n: str) -> str:
    n = n.replace(",", "")
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n.lstrip("0") or "0"


def ungrounded_numbers(text: str, facts: str) -> list[str]:
    """Numbers in ``text`` that never appear in ``facts`` (commas and .00 ignored)."""
    known = {_norm(n) for n in _NUMBER.findall(facts)}
    return sorted({n for n in _NUMBER.findall(text) if _norm(n) not in known})


class GroundedStream:
    """Passes the model's tokens to the screen, but never a number no tool returned.

    A run of digits is held back until it is complete (the next character is not part of
    a number); a grounded number is released, an ungrounded one stops the stream - the
    final, checked reply then replaces the draft. So an invented figure is never shown,
    not even for a moment.
    """

    def __init__(self, facts: str) -> None:
        self.known = {_norm(n) for n in _NUMBER.findall(facts)}
        self.pending = ""
        self.blocked = False

    def learn(self, facts: str) -> None:
        self.known |= {_norm(n) for n in _NUMBER.findall(facts)}

    def reset(self) -> None:
        self.pending, self.blocked = "", False

    def feed(self, text: str) -> str:
        if self.blocked:
            return ""
        self.pending += text
        tail = re.search(r"[\d,.]*$", self.pending).start()  # a number may still go on
        ready, self.pending = self.pending[:tail], self.pending[tail:]
        return self._release(ready)

    def flush(self) -> str:
        ready, self.pending = self.pending, ""
        return "" if self.blocked else self._release(ready)

    def _release(self, text: str) -> str:
        for m in _NUMBER.finditer(text):
            if _norm(m.group(0)) not in self.known:
                self.blocked = True
                return text[: m.start()]
        return text


def _facts_text(facts: list[dict[str, Any]]) -> str:
    lines = []
    for f in facts:
        if f["is_error"]:
            lines.append(f["content"])
            continue
        for key, value in json.loads(f["content"]).items():
            if value not in (None, [], {}):
                lines.append(f"{key.replace('_', ' ')}: {value}")
    return "\n".join(lines)


def _stop_reason(message: AIMessage) -> str:
    meta = message.response_metadata or {}
    if meta.get("stop_reason") == "refusal":  # Claude
        return "refusal"
    if meta.get("stop_reason") == "max_tokens" or meta.get("done_reason") == "length":
        return "max_tokens"
    return "tool_use" if message.tool_calls else "end"


class ChatAgent:
    def __init__(self, model: BaseChatModel | None, max_steps: int | None = None,
                 checkpointer: BaseCheckpointSaver | None = None) -> None:  # fmt: skip
        self.model = model
        self.max_steps = max_steps or get_settings().llm_max_steps
        self.memory = checkpointer or InMemorySaver()
        self.graph = self._build().compile(checkpointer=self.memory)
        self._threads: dict[int, OrderedDict[str, None]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ graph

    def _build(self) -> StateGraph:
        g = StateGraph(ChatState, context_schema=TurnContext)
        g.add_node("route", self._route)
        g.add_node("agent", self._agent)
        g.add_node("tools", self._tools)
        g.add_node("ground", self._ground)
        g.add_node("finish", self._finish)
        g.add_edge(START, "route")
        g.add_conditional_edges("route", lambda s: "finish" if s["stop"] == "routed" else "agent",
                                ["finish", "agent"])  # fmt: skip
        g.add_conditional_edges("agent", lambda s: {"tool_use": "tools", "off": "finish"}.get(
            s["stop"], "ground"), ["tools", "ground", "finish"])  # fmt: skip
        g.add_conditional_edges("tools", lambda s: "ground" if s["steps"] >= self.max_steps
                                else "agent", ["agent", "ground"])  # fmt: skip
        g.add_conditional_edges("ground", lambda s: "agent" if s["stop"] == "retry" else "finish",
                                ["agent", "finish"])  # fmt: skip
        g.add_edge("finish", END)
        return g

    def _route(self, state: ChatState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
        from payroll_copilot.chat.routed import handle
        from payroll_copilot.chat.router import apply_followup, context_of, route

        ctx = runtime.context.ctx
        r = apply_followup(route(ctx.session, ctx.user, runtime.context.message, ctx.today),
                           ctx.chat_context)  # fmt: skip
        result = handle(ctx, r)
        if result is None:
            return {}
        if context_of(r):
            ctx.chat_context = context_of(r)
        ctx.audit_rows.append(audit.entry(
            "chat.route", actor=ctx.user, entity="intent", entity_id=r.intent,
            new={"kind": r.kind, "employee": r.employee, "months": [str(m) for m in r.months],
                 "component": r.component, "tool": result.tool},
            request_id=ctx.request_id))  # fmt: skip
        return {"messages": [AIMessage(content=result.text)], "reply": result.text,
                "cards": result.cards, "stop": "routed", "model": "intent-router",
                "tools_used": [f"router:{r.intent}", *([result.tool] if result.tool else [])]}

    def _agent(self, state: ChatState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
        if runtime.context.model is None:
            return {"reply": OFF_TEXT, "stop": "off"}
        prompt = [SystemMessage(content=runtime.context.system), *state["messages"]]
        if state["stop"] == "retry":  # one-off hint, not kept in the conversation
            prompt.append(HumanMessage(content=RETRY_HINT))
        runtime.stream_writer({"status": "Double-checking the figures…"
                               if state["stop"] == "retry" else "Thinking…"})
        try:
            message = runtime.context.model.invoke(prompt)
        except Exception as exc:
            error = unavailable(exc, self.model.name or "")
            if error is not None:
                raise error from exc
            raise
        stop = _stop_reason(message)
        if stop in ("refusal", "max_tokens"):
            # never keep a cut-off assistant message (it may hold unanswered tool calls)
            message = AIMessage(content=REFUSAL_TEXT if stop == "refusal" else message.text)
        return {"messages": [message], "steps": state["steps"] + 1, "stop": stop,
                "model": self.model.name}  # fmt: skip

    def _tools(self, state: ChatState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
        tools = {t.name: t for t in runtime.context.tools}
        results: list[ToolMessage] = []
        cards, facts, used = list(state["cards"]), list(state["facts"]), list(state["tools_used"])
        runtime.stream_writer({"reset": True})  # text written before a tool call is a draft
        for call in state["messages"][-1].tool_calls:
            runtime.stream_writer({"status": TOOL_STATUS.get(call["name"], "Working…")})
            tool = tools.get(call["name"])
            if tool is not None:
                result = tool.invoke({**call, "type": "tool_call"})
                artifact = result.artifact or {}
            else:  # unknown tool: run_tool audits and rejects it
                try:
                    run_tool(runtime.context.ctx, call["name"], call["args"])
                except ToolError as exc:
                    result = ToolMessage(content=f"ERROR: {exc}", tool_call_id=call["id"],
                                         name=call["name"])  # fmt: skip
                artifact = {"cards": [], "is_error": True}
            if artifact.get("is_error"):
                result.status = "error"
            cards.extend(artifact.get("cards", []))
            facts.append({"content": result.content.removeprefix("ERROR: ")
                          if artifact.get("is_error") else result.content,
                          "is_error": bool(artifact.get("is_error"))})  # fmt: skip
            used.append(call["name"])
            results.append(result)
        return {"messages": results, "cards": cards, "facts": facts, "tools_used": used}

    def _ground(self, state: ChatState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
        facts, warnings = state["facts"], list(state["warnings"])
        if state["stop"] == "tool_use":  # the step limit ended the loop
            warnings.append(f"stopped after {self.max_steps} model steps")
            text = _facts_text(facts) or "Sorry, I could not complete that. Please try again."
            return {"reply": text, "warnings": warnings}
        last = state["messages"][-1]
        text = original = last.text.strip()
        if state["stop"] == "end":
            # figures may come from this turn's tools, the question, the prompt or the earlier
            # (already grounded) conversation
            known = " ".join([f["content"] for f in facts]
                             + [m.text for m in state["messages"][:-1]]
                             + [runtime.context.message, runtime.context.system])  # fmt: skip
            invented = ungrounded_numbers(text, known)
            if invented and not state["retried"] and state["steps"] < self.max_steps:
                warnings.append(f"model retried: numbers not from tools {invented}")
                runtime.stream_writer({"reset": True})
                return {"messages": [RemoveMessage(id=last.id)], "stop": "retry",
                        "retried": True, "warnings": warnings}  # fmt: skip
            if invented:
                warnings.append(f"model text replaced: numbers not from tools {invented}")
                text = ("Here are the exact figures from payroll:\n" + _facts_text(facts)
                        if facts else UNRELIABLE_TEXT)  # fmt: skip
            elif not facts and _CHANGE_CLAIM.search(text):
                warnings.append("model text replaced: claimed a change without a tool")
                text = NO_CHANGE_TEXT
        if not text:
            text = "Here you go." if state["cards"] else "Sorry, I could not answer that."
        update: dict[str, Any] = {"reply": text, "warnings": warnings}
        if text != original:  # remember what the user saw, never the rejected draft
            update["messages"] = [AIMessage(content=text, id=last.id)]
        return update

    def _finish(self, state: ChatState) -> dict[str, Any]:
        messages = state["messages"]
        humans = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
        if len(humans) <= MAX_TURNS_KEPT:
            return {}
        return {"messages": [RemoveMessage(id=m.id) for m in messages[: humans[-MAX_TURNS_KEPT]]]}

    # ------------------------------------------------------------------ public

    def system_prompt(self, ctx: ToolContext) -> str:
        from payroll_copilot.core.dates import FinancialYear, PayrollPeriod

        user = ctx.user
        return SYSTEM_PROMPT.format(
            company=get_settings().company_name, today=ctx.today.isoformat(),
            fy=FinancialYear.from_date(ctx.today), name=user.full_name, code=user.employee_code,
            role=user.role.value, hr_note=HR_NOTE if is_staff(user) else "",
            last_month=PayrollPeriod.from_date(ctx.today).previous(),
        )  # fmt: skip

    def _thread(self, user_id: int, conversation_id: str) -> dict[str, Any]:
        """LangGraph thread of this user's conversation; old conversations are dropped."""
        with self._lock:
            threads = self._threads.setdefault(user_id, OrderedDict())
            threads[conversation_id] = None
            threads.move_to_end(conversation_id)
            while len(threads) > MAX_CONVERSATIONS_PER_USER:
                old, _ = threads.popitem(last=False)
                self.memory.delete_thread(f"{user_id}:{old}")
        return {"configurable": {"thread_id": f"{user_id}:{conversation_id}"}}

    def _prepare(
        self, ctx: ToolContext, message: str, conversation_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], TurnContext]:
        config = self._thread(ctx.user.id, conversation_id)
        new: list[AnyMessage] = []
        if not self.graph.get_state(config).values.get("messages"):
            # after a restart the memory is rebuilt from the saved history
            for question, answer in ctx.history[-MAX_TURNS_KEPT:]:
                new += [HumanMessage(content=question), AIMessage(content=answer)]
        new.append(HumanMessage(content=message))
        tools = langchain_tools(ctx)
        allowed = {t.name for t in tools_for(ctx.user.role)}
        bound = (self.model.bind_tools([t for t in tools if t.name in allowed])
                 if self.model is not None else None)  # fmt: skip
        turn = TurnContext(ctx=ctx, message=message, system=self.system_prompt(ctx),
                           tools=tools, model=bound)  # fmt: skip
        payload = {"messages": new, "reply": "", "cards": [], "tools_used": [], "facts": [],
                   "steps": 0, "stop": "", "retried": False, "model": None, "warnings": []}
        return payload, config, turn

    @staticmethod
    def _reply(conversation_id: str, state: dict[str, Any]) -> ChatReply:
        return ChatReply(
            conversation_id=conversation_id,
            messages=[{"type": "text", "text": state["reply"]}, *state["cards"]],
            tools_used=state["tools_used"], model=state["model"], warnings=state["warnings"],
        )  # fmt: skip

    def ask(self, ctx: ToolContext, message: str, conversation_id: str) -> ChatReply:
        payload, config, turn = self._prepare(ctx, message, conversation_id)
        return self._reply(conversation_id, self.graph.invoke(payload, config, context=turn))

    def stream(self, ctx: ToolContext, message: str,
               conversation_id: str) -> Iterator[dict[str, Any]]:  # fmt: skip
        """The same turn as ``ask``, as events while the graph runs:

        * ``{"event": "status", "text"}``  - what the assistant is doing now;
        * ``{"event": "token", "text"}``   - the answer as the model writes it (never a
          number no tool returned, see GroundedStream);
        * ``{"event": "reset"}``           - drop the draft text (a tool runs / a retry);
        * ``{"event": "done", "reply"}``   - the final, checked ChatReply (authoritative).
        """
        payload, config, turn = self._prepare(ctx, message, conversation_id)
        earlier = [m.text for m in self.graph.get_state(config).values.get("messages", [])]
        safe = GroundedStream(" ".join([*earlier, *(m.text for m in payload["messages"]),
                                        turn.system]))  # fmt: skip
        for mode, data in self.graph.stream(payload, config, context=turn,
                                            stream_mode=["custom", "messages", "updates"]):
            if mode == "custom":
                if data.get("reset"):
                    safe.reset()
                    yield {"event": "reset"}
                if data.get("status"):
                    yield {"event": "status", "text": data["status"]}
            elif mode == "messages":
                chunk, meta = data
                agent_text = (chunk.text if meta.get("langgraph_node") == "agent"
                              and isinstance(chunk, AIMessageChunk) else "")  # fmt: skip
                if text := safe.feed(agent_text):
                    yield {"event": "token", "text": text}
            elif mode == "updates":
                for update in (data or {}).values():
                    for fact in (update or {}).get("facts") or []:
                        safe.learn(fact["content"])
        if text := safe.flush():
            yield {"event": "token", "text": text}
        yield {"event": "done", "reply": self._reply(conversation_id,
                                                     self.graph.get_state(config).values)}

    def forget(self, user_id: int, conversation_id: str) -> bool:
        """Drop the model's memory of a conversation; True if there was any."""
        config = {"configurable": {"thread_id": f"{user_id}:{conversation_id}"}}
        existed = bool(self.graph.get_state(config).values.get("messages"))
        self.memory.delete_thread(f"{user_id}:{conversation_id}")
        with self._lock:
            self._threads.get(user_id, OrderedDict()).pop(conversation_id, None)
        return existed
