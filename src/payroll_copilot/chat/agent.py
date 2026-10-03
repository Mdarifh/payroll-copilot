"""The chat loop: model picks tools, the backend runs them, the model words the answer.

Guarantees, whatever the model does:
* tools are limited to the user's role and run with the logged-in user (chat.tools);
* cards (payslip_card, ...) are built from tool output, never from model text;
* if the model writes a number that no tool returned, its text is replaced by the
  tool facts (numbers are never invented by the LLM).
"""

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from payroll_copilot.auth.permissions import is_staff
from payroll_copilot.chat.tools import ToolContext, ToolError, run_tool, tools_for
from payroll_copilot.core.config import get_settings
from payroll_copilot.llm.base import ChatModel, ModelTurn, ToolDefinition, ToolOutcome
from payroll_copilot.services import audit

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
MAX_TURNS_KEPT = 10  # user turns remembered per conversation
MAX_CONVERSATIONS_PER_USER = 20

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
- If a tool returns an error, explain it plainly. For policy questions you cannot answer
  with a tool, say you don't know yet and suggest contacting HR.
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


class ConversationStore:
    """In-memory conversation history per user (lost on restart; enough for a demo)."""

    def __init__(self) -> None:
        self._data: dict[int, OrderedDict[str, list[list[Any]]]] = {}
        self._lock = threading.Lock()

    def turns(self, user_id: int, conversation_id: str) -> list[list[Any]]:
        with self._lock:
            convs = self._data.setdefault(user_id, OrderedDict())
            turns = convs.setdefault(conversation_id, [])
            convs.move_to_end(conversation_id)
            while len(convs) > MAX_CONVERSATIONS_PER_USER:
                convs.popitem(last=False)
            return turns

    def save(self, user_id: int, conversation_id: str, turns: list[list[Any]]) -> None:
        with self._lock:
            self._data.setdefault(user_id, OrderedDict())[conversation_id] = (
                turns[-MAX_TURNS_KEPT:])

    def clear(self, user_id: int, conversation_id: str) -> bool:
        with self._lock:
            return self._data.get(user_id, OrderedDict()).pop(conversation_id, None) is not None


def ungrounded_numbers(text: str, facts: str) -> list[str]:
    """Numbers in ``text`` that never appear in ``facts`` (commas and .00 ignored)."""

    def norm(n: str) -> str:
        n = n.replace(",", "")
        if "." in n:
            n = n.rstrip("0").rstrip(".")
        return n.lstrip("0") or "0"

    known = {norm(n) for n in _NUMBER.findall(facts)}
    return sorted({n for n in _NUMBER.findall(text) if norm(n) not in known})


def _facts_text(outcomes: list[ToolOutcome]) -> str:
    lines = []
    for o in outcomes:
        if o.is_error:
            lines.append(o.content)
            continue
        for key, value in json.loads(o.content).items():
            if value not in (None, [], {}):
                lines.append(f"{key.replace('_', ' ')}: {value}")
    return "\n".join(lines)


class ChatAgent:
    def _previous(self, ctx: ToolContext, conversation_id: str) -> list[list[Any]]:
        """The model's memory of this conversation; after a restart it is rebuilt from the
        saved history (text of each exchange)."""
        previous = self.store.turns(ctx.user.id, conversation_id)
        if not previous and ctx.history and self.model is not None:
            previous = [[self.model.user_message(q),
                         self.model.assistant_message(ModelTurn(text=a))]
                        for q, a in ctx.history]
        return previous

    def _routed(self, ctx: ToolContext, message: str, conversation_id: str) -> ChatReply | None:
        """Intent router first: payroll questions it recognises are answered from the engine
        directly (exact, fast, no LLM arithmetic); writes only produce a preview."""
        from payroll_copilot.chat.routed import handle
        from payroll_copilot.chat.router import apply_followup, context_of, route

        r = apply_followup(route(ctx.session, ctx.user, message, ctx.today), ctx.chat_context)
        result = handle(ctx, r)
        if result is None:
            return None
        if context_of(r):
            ctx.chat_context = context_of(r)
        ctx.audit_rows.append(audit.entry(
            "chat.route", actor=ctx.user, entity="intent", entity_id=r.intent,
            new={"kind": r.kind, "employee": r.employee, "months": [str(m) for m in r.months],
                 "component": r.component, "tool": result.tool},
            request_id=ctx.request_id))  # fmt: skip
        if self.model is not None:  # keep the conversation for follow-up questions
            previous = self._previous(ctx, conversation_id)
            turn = [self.model.user_message(message),
                    self.model.assistant_message(ModelTurn(text=result.text))]
            self.store.save(ctx.user.id, conversation_id, [*previous, turn])
        return ChatReply(
            conversation_id=conversation_id,
            messages=[{"type": "text", "text": result.text}, *result.cards],
            tools_used=[f"router:{r.intent}", *([result.tool] if result.tool else [])],
            model="intent-router",
        )  # fmt: skip

    def __init__(self, model: ChatModel | None, store: ConversationStore | None = None,
                 max_steps: int | None = None) -> None:  # fmt: skip
        self.model = model
        self.store = store or ConversationStore()
        self.max_steps = max_steps or get_settings().llm_max_steps

    def system_prompt(self, ctx: ToolContext) -> str:
        from payroll_copilot.core.dates import FinancialYear, PayrollPeriod

        user = ctx.user
        return SYSTEM_PROMPT.format(
            company=get_settings().company_name, today=ctx.today.isoformat(),
            fy=FinancialYear.from_date(ctx.today), name=user.full_name, code=user.employee_code,
            role=user.role.value, hr_note=HR_NOTE if is_staff(user) else "",
            last_month=PayrollPeriod.from_date(ctx.today).previous(),
        )  # fmt: skip

    def ask(self, ctx: ToolContext, message: str, conversation_id: str) -> ChatReply:
        routed = self._routed(ctx, message, conversation_id)
        if routed is not None:
            return routed
        if self.model is None:
            return ChatReply(conversation_id, [{"type": "text", "text":
                "The AI assistant is switched off (LLM_PROVIDER=none). Please contact HR."}])
        model = self.model
        user_id = ctx.user.id
        previous = self._previous(ctx, conversation_id)
        turn: list[Any] = [model.user_message(message)]
        tools = [ToolDefinition(t.name, t.description, t.parameters)
                 for t in tools_for(ctx.user.role)]  # fmt: skip
        system = self.system_prompt(ctx)
        outcomes: list[ToolOutcome] = []
        cards: list[dict[str, Any]] = []
        warnings: list[str] = []
        final: ModelTurn | None = None

        for _ in range(self.max_steps):
            history = [m for t in previous for m in t] + turn
            result = model.complete(system, history, tools)
            if result.stop in ("refusal", "max_tokens"):
                # never keep a cut-off assistant message (it may hold unanswered tool calls)
                final = ModelTurn(text="Sorry, I can't help with that request. Please contact HR."
                                  if result.stop == "refusal" else result.text, stop=result.stop)
                turn.append(model.assistant_message(ModelTurn(text=final.text)))
                break
            turn.append(model.assistant_message(result))
            if not result.tool_calls:
                final = result
                break
            step_outcomes = []
            for call in result.tool_calls:
                try:
                    out = run_tool(ctx, call.name, call.arguments)
                    cards.extend(out.cards)
                    step_outcomes.append(ToolOutcome(call, json.dumps(out.content)))
                except ToolError as exc:
                    step_outcomes.append(ToolOutcome(call, str(exc), is_error=True))
            outcomes.extend(step_outcomes)
            turn.extend(model.tool_result_messages(step_outcomes))

        if final is None:
            warnings.append(f"stopped after {self.max_steps} model steps")
            text = _facts_text(outcomes) or "Sorry, I could not complete that. Please try again."
        else:
            text = final.text.strip()
            facts = " ".join(o.content for o in outcomes) + " " + message + " " + system
            invented = ungrounded_numbers(text, facts) if final.stop == "end" else []
            if invented:
                warnings.append(f"model text replaced: numbers not from tools {invented}")
                text = ("Here are the exact figures from payroll:\n" + _facts_text(outcomes)
                        if outcomes else "Sorry, I could not answer that reliably.")  # fmt: skip
            if not text:
                text = "Here you go." if cards else "Sorry, I could not answer that."

        self.store.save(user_id, conversation_id, [*previous, turn])
        return ChatReply(
            conversation_id=conversation_id,
            messages=[{"type": "text", "text": text}, *cards],
            tools_used=[o.call.name for o in outcomes],
            model=model.name, warnings=warnings,
        )  # fmt: skip
