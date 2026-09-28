"""The Payroll Copilot LangGraph workflow.

START → intent_detection → entity_extraction → route
  ├─ clarify ─────────────────────────────────────────────────────────────→ END
  ├─ knowledge → retrieve_rules → explanation → final_response ───────────→ END
  ├─ general → general_agent (LLM tool-calling, or help text) → final_response → END
  └─ calculation routes → retrieve_data → [retrieve_rules] → calculation
                        → validation → explanation → final_response ──────→ END

Numbers only ever come from tools (the deterministic engine). The LLM, when
configured, classifies nothing and computes nothing: it explains tool output,
and every number it writes must appear in that output or its text is replaced
by the deterministic explanation.
"""

from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from payroll_copilot.agent.actions import parse_action
from payroll_copilot.agent.entities import extract_entities
from payroll_copilot.agent.intents import (
    ACTION_INTENTS,
    INTENT_MODULE,
    NEEDS_EMPLOYEE_AND_PERIOD,
    NEEDS_EMPLOYEE_ONLY,
    NEEDS_PERIOD_ONLY,
    WHY_WORDS,
    Intent,
    detect_intent,
    knowledge_module,
)
from payroll_copilot.agent.state import AgentState, ToolRun
from payroll_copilot.audit.context import current_actor
from payroll_copilot.core.exceptions import PayrollCopilotError
from payroll_copilot.domain.enums import ActionType
from payroll_copilot.rag.chain import ungrounded_numbers
from payroll_copilot.services.approval_describe import describe as describe_action

TOOL_PLAN: dict[Intent, str] = {
    Intent.ATTENDANCE: "get_attendance",
    Intent.SALARY: "calculate_salary",
    Intent.STRUCTURE: "get_salary_structure",
    Intent.PAYSLIP: "get_payslip",
    Intent.PF: "calculate_pf",
    Intent.PF_COMPARE: "compare_pf_rules",
    Intent.ESIC: "calculate_esic",
    Intent.PT: "calculate_pt",
    Intent.TDS: "calculate_tds",
    Intent.ARREAR: "calculate_arrear",
    Intent.FNF: "calculate_fnf",
    Intent.QA_EMPLOYEE: "compare_expected_actual",
    Intent.QA_PERIOD: "run_payroll_qa",
    Intent.EMPLOYEE: "get_employee",
}

HELP_TEXT = (
    "Main payroll ke sawaalon mein madad kar sakta hoon. Examples:\n"
    "- ARIF-112 ka PF September 2026 mein kitna hai?\n"
    "- EMP-002 ke paid days 2026-09 mein kitne hain? / Why was salary reduced?\n"
    "- EMP-004 ka arrear Sep 2026 / EMP-006 ka F&F\n"
    "- Validate September 2026 payroll / EMP-008 expected vs actual 2026-09\n"
    "- What is the PF wage ceiling in 2026-08? (policy)"
)

EXPLAIN_PROMPT = """You explain payroll results to an HR/payroll user.
Use ONLY the tool output below. Do not calculate or invent any number; every
amount you mention must appear in the tool output. Mention rule versions and
warnings that matter. Reply in the user's language (English or Hinglish),
briefly, in plain text.

Tool output:
{context}"""

AGENT_PROMPT = """You are a payroll assistant. Use the tools for every number; never compute
payroll values yourself. Ask for an employee code (like EMP-002) or payroll month (YYYY-MM)
if you need one. Reply in the user's language (English or Hinglish)."""


class ActionHandler(Protocol):
    """Bridge from the graph to the approval workflow (keeps the graph DB-agnostic)."""

    def preview(self, action: ActionType, payload: dict[str, Any]) -> dict[str, Any]: ...

    def submit(self, action: ActionType, payload: dict[str, Any], actor: str) -> dict: ...


def _run_tool(tool: BaseTool, args: dict[str, Any]) -> ToolRun:
    msg = tool.invoke({"name": tool.name, "args": args, "id": f"{tool.name}-call",
                       "type": "tool_call"})  # fmt: skip
    content = str(msg.content)
    artifact = getattr(msg, "artifact", None)
    ok = not content.startswith("ERROR")
    return ToolRun(name=tool.name, args=args, content=content, artifact=artifact, ok=ok)


def build_graph(
    tools: Iterable[BaseTool],
    llm: BaseChatModel | None = None,
    *,
    directory: Callable[[], list[tuple[str, str]]] = lambda: [],
    known_states: Iterable[str] = (),
    checkpointer: BaseCheckpointSaver | None = None,
    actions: "ActionHandler | None" = None,
):
    tools_by_name = {t.name: t for t in tools}
    states = set(known_states)

    # ---------------------------------------------------------------- nodes

    def intent_detection(state: AgentState) -> dict:
        question = state["messages"][-1].content if state.get("messages") else ""
        code_in_text = extract_entities(question, directory(), states).employee_code is not None
        intent, matched = detect_intent(question, code_in_text)
        return {
            "question": question, "intent": intent.value, "matched": matched,
            "tool_runs": [], "policy": None, "checks": [], "warnings": [], "sources": [],
            "employee": None, "explanation": "", "used_llm": False, "missing": [],
            "needs_rules": intent is Intent.KNOWLEDGE or bool(WHY_WORDS.search(question)),
        }  # fmt: skip

    def entity_extraction(state: AgentState) -> dict:
        entities = extract_entities(state["question"], directory(), states)
        intent = Intent(state["intent"])
        wants_employee = intent in NEEDS_EMPLOYEE_AND_PERIOD | NEEDS_EMPLOYEE_ONLY
        wants_period = intent in NEEDS_EMPLOYEE_AND_PERIOD | NEEDS_PERIOD_ONLY
        if wants_employee and not entities.employee_code and state.get("last_employee_code"):
            entities.employee_code, entities.employee_source = state["last_employee_code"], "memory"
        if wants_period and not entities.period and state.get("last_period"):
            entities.period, entities.period_source = state["last_period"], "memory"
        missing = []
        if wants_employee and not entities.employee_code:
            missing.append("employee_code")
        if wants_period and not entities.period:
            missing.append("period")
        module = INTENT_MODULE.get(intent) or (
            knowledge_module(state["question"]) if intent is Intent.KNOWLEDGE else None
        )
        return {"entities": entities.as_dict(), "missing": missing, "module": module}

    def route(state: AgentState) -> str:
        intent = Intent(state["intent"])
        if state["missing"]:
            return "clarify"
        if intent in ACTION_INTENTS:
            return "prepare_action"
        if intent is Intent.KNOWLEDGE:
            return "retrieve_rules"
        if intent is Intent.GENERAL:
            return "general_agent"
        return "retrieve_data"

    def clarify(state: AgentState) -> dict:
        asks = {
            "employee_code": "kis employee ke liye? Employee code batayein (jaise EMP-002)",
            "period": "kaunsa payroll month? (YYYY-MM, jaise 2026-09)",
        }
        notes = state["entities"].get("notes") or []
        text = "Thodi aur jaankari chahiye: " + "; ".join(asks[m] for m in state["missing"])
        if notes:
            text += "\n(" + "; ".join(notes) + ")"
        return {"route": "clarify", "answer": text, "messages": [AIMessage(content=text)]}

    def prepare_action(state: AgentState) -> dict:
        """Parse the write action, preview its impact and ask the user to confirm."""
        def done(text: str) -> dict:
            return {"route": "action", "answer": text, "pending_action": None,
                    "messages": [AIMessage(content=text)]}  # fmt: skip

        if actions is None:
            return done("Write actions are not enabled in this session.")
        intent = Intent(state["intent"])
        action, payload, problem = parse_action(intent, state["question"], state["entities"],
                                                current_actor())  # fmt: skip
        if problem:
            return done("Thodi aur jaankari chahiye: " + problem)
        try:
            preview = actions.preview(action, payload)
        except PayrollCopilotError as exc:
            return done(f"Ye action nahi ho sakta: {exc}")
        pending = {"action": action.value, "payload": payload, "preview": preview,
                   "description": describe_action(action, payload)}  # fmt: skip
        impact = ", ".join(f"{k}: {v}" for k, v in preview.items())
        text = (f"Ye write action hai, iske liye approval chahiye:\n  {pending['description']}\n"
                f"  Impact: {impact}\n"
                "Approval request submit karun? (yes / no)")  # fmt: skip
        return {"route": "action_pending", "answer": text, "pending_action": pending,
                "messages": [AIMessage(content=text)]}  # fmt: skip

    def submit_action(state: AgentState) -> dict:
        pending = state["pending_action"]
        decision = interrupt(pending)  # the graph pauses here until the user answers
        if not decision.get("confirm"):
            text = "Theek hai, action cancel kar diya. Kuch bhi change nahi hua."
        else:
            try:
                approval = actions.submit(ActionType(pending["action"]), pending["payload"],
                                          decision.get("actor") or current_actor())  # fmt: skip
                text = (f"Approval request #{approval['id']} ban gaya (PENDING): "
                        f"{pending['description']}.\nKoi approver (aap nahi) ise approve karega: "
                        f"payroll-copilot approval approve {approval['id']} --as <approver>")
            except PayrollCopilotError as exc:
                text = f"Request nahi ban paya: {exc}"
        return {"route": "action", "answer": text, "pending_action": None,
                "messages": [AIMessage(content=text)]}  # fmt: skip

    def retrieve_data(state: AgentState) -> dict:
        code = state["entities"].get("employee_code")
        if not code or "get_employee" not in tools_by_name:
            return {"route": state["intent"]}
        run = _run_tool(tools_by_name["get_employee"], {"employee_code": code})
        employee = (run["artifact"] or {}).get("employee") if run["ok"] else None
        warnings = [] if run["ok"] else [run["content"]]
        return {"route": state["intent"], "employee": employee, "warnings": warnings}

    def retrieve_rules(state: AgentState) -> dict:
        tool = tools_by_name.get("search_payroll_policy")
        if tool is None:
            return {"route": state.get("route") or state["intent"]}
        entities = state["entities"]
        state_code = entities.get("state") or (state.get("employee") or {}).get("work_state")
        args: dict[str, Any] = {"query": state["question"][:500], "k": 3}
        if state.get("module"):
            args["module"] = state["module"]
        if entities.get("period"):
            args["period"] = entities["period"]
        if state_code and state.get("module") in ("pt", "lwf"):
            args["state"] = state_code
        run = _run_tool(tool, args)
        artifact = run["artifact"] or {}
        passages = run["content"].split("\n\nSources:\n")[0]  # sources are listed once, at the end
        return {
            "route": state.get("route") or state["intent"],
            "policy": {"content": passages, "found": artifact.get("found", False)},
            "sources": artifact.get("sources", []),
        }

    def calculation(state: AgentState) -> dict:
        intent = Intent(state["intent"])
        entities = state["entities"]
        args: dict[str, Any] = {}
        if intent in NEEDS_EMPLOYEE_AND_PERIOD | NEEDS_EMPLOYEE_ONLY:
            args["employee_code"] = entities["employee_code"]
        if intent in NEEDS_EMPLOYEE_AND_PERIOD | NEEDS_PERIOD_ONLY:
            args["period"] = entities["period"]
        run = _run_tool(tools_by_name[TOOL_PLAN[intent]], args)
        return {"tool_runs": [run]}

    def validation(state: AgentState) -> dict:
        checks, warnings = [], list(state.get("warnings", []))
        for run in state["tool_runs"]:
            if not run["ok"]:
                warnings.append(run["content"])
                continue
            payslip = (run["artifact"] or {}).get("payslip")
            if payslip:
                gross = Decimal(payslip["gross_earnings"])
                ded = Decimal(payslip["total_deductions"])
                net = Decimal(payslip["net_pay"])
                if gross - ded != net:
                    warnings.append(f"Payslip check failed: {gross} − {ded} ≠ {net}")
                else:
                    checks.append("net pay = gross − deductions ✓")
            if "UNVERIFIED" in run["content"]:
                checks.append("uses UNVERIFIED rule versions (verify before production)")
        return {"checks": checks, "warnings": warnings}

    def explanation(state: AgentState) -> dict:
        parts = [run["content"] for run in state.get("tool_runs", [])]
        policy = state.get("policy")
        if policy:
            label = "" if state["intent"] == Intent.KNOWLEDGE.value else "Related policy:\n"
            parts.append(label + policy["content"])
        context = "\n\n".join(parts)
        deterministic = context or HELP_TEXT
        if llm is None or not context:
            return {"explanation": deterministic, "used_llm": False}
        text = llm.invoke([
            SystemMessage(content=EXPLAIN_PROMPT.format(context=context)),
            HumanMessage(content=state["question"]),
        ]).content  # fmt: skip
        invented = ungrounded_numbers(str(text), context + " " + state["question"])
        if invented:
            return {
                "explanation": deterministic, "used_llm": False,
                "warnings": [*state.get("warnings", []),
                             f"LLM explanation discarded: numbers not in tool output {invented}"],
            }  # fmt: skip
        return {"explanation": str(text), "used_llm": True}

    def general_agent(state: AgentState) -> dict:
        if llm is None:
            return {"route": "general", "explanation": HELP_TEXT, "used_llm": False}
        model = llm.bind_tools(list(tools_by_name.values()))
        messages: list = [
            SystemMessage(content=AGENT_PROMPT),
            HumanMessage(content=state["question"]),
        ]
        runs: list[ToolRun] = []
        ai = None
        for _ in range(5):
            ai = model.invoke(messages)
            messages.append(ai)
            if not getattr(ai, "tool_calls", None):
                break
            for call in ai.tool_calls:
                tool = tools_by_name.get(call["name"])
                if tool is None:
                    messages.append(ToolMessage(content=f"ERROR: unknown tool {call['name']}",
                                                tool_call_id=call["id"]))  # fmt: skip
                    continue
                run = _run_tool(tool, call["args"])
                runs.append(run)
                messages.append(ToolMessage(content=run["content"], tool_call_id=call["id"]))
        text = str(ai.content) if ai is not None else HELP_TEXT
        context = "\n\n".join(r["content"] for r in runs)
        warnings = []
        invented = ungrounded_numbers(text, context + " " + state["question"]) if runs else []
        if invented:
            warnings.append(f"LLM answer discarded: numbers not in tool output {invented}")
            text = context
        return {"route": "general", "tool_runs": runs, "explanation": text or HELP_TEXT,
                "used_llm": not invented, "warnings": warnings}  # fmt: skip

    def final_response(state: AgentState) -> dict:
        answer = state.get("explanation") or HELP_TEXT
        if state.get("sources"):
            answer += "\n\nSources:\n" + "\n".join(state["sources"])
        warnings = list(dict.fromkeys(state.get("warnings", [])))
        if warnings:
            answer += "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings[:6])
        entities = state.get("entities") or {}
        memory = {}
        if entities.get("employee_code"):
            memory["last_employee_code"] = entities["employee_code"]
        if entities.get("period"):
            memory["last_period"] = entities["period"]
        return {"answer": answer, "warnings": warnings, "messages": [AIMessage(content=answer)],
                **memory}  # fmt: skip

    # ---------------------------------------------------------------- wiring

    graph = StateGraph(AgentState)
    for name, node in (
        ("intent_detection", intent_detection), ("entity_extraction", entity_extraction),
        ("clarify", clarify), ("retrieve_data", retrieve_data),
        ("retrieve_rules", retrieve_rules), ("calculation", calculation),
        ("validation", validation), ("explanation", explanation),
        ("general_agent", general_agent), ("final_response", final_response),
        ("prepare_action", prepare_action), ("submit_action", submit_action),
    ):  # fmt: skip
        graph.add_node(name, node)
    graph.add_edge(START, "intent_detection")
    graph.add_edge("intent_detection", "entity_extraction")
    graph.add_conditional_edges(
        "entity_extraction", route,
        ["clarify", "retrieve_rules", "general_agent", "retrieve_data", "prepare_action"],
    )  # fmt: skip
    graph.add_edge("clarify", END)
    graph.add_conditional_edges(
        "prepare_action", lambda s: "submit_action" if s.get("pending_action") else END,
        ["submit_action", END],
    )  # fmt: skip
    graph.add_edge("submit_action", END)
    graph.add_conditional_edges(
        "retrieve_data",
        lambda s: "retrieve_rules" if s.get("needs_rules") else "calculation",
        ["retrieve_rules", "calculation"],
    )
    graph.add_conditional_edges(
        "retrieve_rules",
        lambda s: "explanation" if s["intent"] == Intent.KNOWLEDGE.value else "calculation",
        ["explanation", "calculation"],
    )
    graph.add_edge("calculation", "validation")
    graph.add_edge("validation", "explanation")
    graph.add_edge("explanation", "final_response")
    graph.add_edge("general_agent", "final_response")
    graph.add_edge("final_response", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver())
