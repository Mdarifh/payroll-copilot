"""The LangGraph workflow end to end (real tools on the seeded DB, fake KB/LLM)."""

from contextlib import contextmanager
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from payroll_copilot.agent.copilot import PayrollCopilot
from payroll_copilot.services.actual_payroll_seed import seed_actual_payroll
from payroll_copilot.tools import ToolContext


class ScriptedLLM(BaseChatModel):
    """Returns queued AIMessages; supports bind_tools so the agent loop can be tested."""

    responses: list[AIMessage] = Field(default_factory=list)
    seen: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(messages)
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


@pytest.fixture
def context(seeded_session, kb):
    @contextmanager
    def sessions():
        yield seeded_session

    return ToolContext(sessions, knowledge_base=lambda: kb)


@pytest.fixture
def copilot(context):
    return PayrollCopilot(context, use_configured_llm=False)


def test_graph_nodes(copilot):
    nodes = set(copilot.graph.get_graph().nodes)
    assert {"intent_detection", "entity_extraction", "clarify", "retrieve_data", "retrieve_rules",
            "calculation", "validation", "explanation", "general_agent",
            "final_response"} <= nodes  # fmt: skip


def test_pf_question_and_follow_up_uses_memory(copilot):
    reply = copilot.ask("ARIF-112 ka PF September 2026 mein kitna hai?", thread_id="t1")
    assert (reply.intent, reply.tools_used) == ("PF", ["calculate_pf"])
    assert "Employee PF        ₹3,000" in reply.answer
    assert reply.artifacts[0]["pf"]["amounts"]["EE_PF"] == "3000"
    assert any("UNVERIFIED" in c for c in reply.checks)

    follow = copilot.ask("aur ESIC?", thread_id="t1")
    assert follow.intent == "ESIC" and follow.tools_used == ["calculate_esic"]
    assert follow.entities["employee_source"] == "memory"
    assert follow.entities["period_source"] == "memory"
    assert "not applicable" in follow.answer

    other_thread = copilot.ask("aur ESIC?", thread_id="t2")
    assert other_thread.route == "clarify"


def test_salary_reduction_by_name_with_policy_context(copilot):
    reply = copilot.ask("Priya ki salary Sep 2026 mein kyun kam hui?", thread_id="t3")
    assert reply.entities["employee_code"] == "EMP-002"
    assert reply.entities["employee_source"] == "name"
    assert "Less: LOP" in reply.answer and "-₹4,000" in reply.answer
    assert reply.sources and "Related policy:" in reply.answer
    assert reply.answer.count("Sources:") == 1


def test_clarify_when_information_missing(copilot):
    reply = copilot.ask("PF kitna hai?", thread_id="t4")
    assert reply.route == "clarify" and reply.tools_used == []
    assert "Employee code batayein" in reply.answer and "payroll month" in reply.answer
    only_period = copilot.ask("EMP-002 ka PF", thread_id="t5")
    assert "payroll month" in only_period.answer and "Employee code" not in only_period.answer


def test_unknown_employee_is_explained(copilot):
    reply = copilot.ask("EMP-999 ka PF 2026-09", thread_id="t6")
    assert reply.route == "clarify" and "EMP-999 not found" in reply.answer


def test_policy_question_is_period_aware(copilot):
    reply = copilot.ask("What is the PF wage ceiling in 2026-08?", thread_id="t7")
    assert reply.intent == "KNOWLEDGE" and reply.tools_used == []
    assert reply.sources and all("EPF v1" in s for s in reply.sources)
    assert "15000" in reply.answer


@pytest.mark.parametrize(("question", "tool", "needle"), [
    ("EMP-006 ka F&F", "calculate_fnf", "NET F&F SETTLEMENT: ₹34,000"),
    ("ARIF-112 PF old vs new rule 2026-09", "compare_pf_rules", "EPF v1"),
    ("EMP-004 arrear Sep 2026", "calculate_arrear", "ARREARS"),
    ("EMP-010 ka payslip 2026-09", "get_payslip", "NET PAY"),
    ("EMP-005 DOJ kya hai", "get_employee", "doj: 2026-09-15"),
])  # fmt: skip
def test_routes(copilot, question, tool, needle):
    reply = copilot.ask(question, thread_id=question)
    assert reply.tools_used == [tool] and needle in reply.answer


def test_payroll_qa(copilot, seeded_session):
    seed_actual_payroll(seeded_session)
    run = copilot.ask("Validate September 2026 payroll", thread_id="qa")
    assert run.intent == "QA_PERIOD" and "with issues: 7" in run.answer
    one = copilot.ask("EMP-008 expected vs actual 2026-09", thread_id="qa2")
    assert one.intent == "QA_EMPLOYEE" and "[SLAB]" in one.answer


def test_general_without_llm_shows_help(copilot):
    reply = copilot.ask("hello", thread_id="g")
    assert reply.route == "general" and "Examples" in reply.answer


class TestWithLLM:
    def test_grounded_explanation_is_used(self, context):
        llm = ScriptedLLM(responses=[AIMessage(content="ARIF-112 ka Employee PF ₹3,000 hai (EPF v2).")])
        reply = PayrollCopilot(context, llm=llm).ask("ARIF-112 ka PF 2026-09", thread_id="l1")
        assert reply.used_llm and reply.answer.startswith("ARIF-112 ka Employee PF ₹3,000")
        system = llm.seen[0][0].content
        assert "Do not calculate or invent any number" in system and "min(30000, 25000)" in system

    def test_invented_number_is_discarded(self, context):
        llm = ScriptedLLM(responses=[AIMessage(content="PF ₹9,999 hai.")])
        reply = PayrollCopilot(context, llm=llm).ask("ARIF-112 ka PF 2026-09", thread_id="l2")
        assert not reply.used_llm and "Employee PF        ₹3,000" in reply.answer
        assert any("discarded" in w and "9,999" in w for w in reply.warnings)

    def test_general_agent_calls_tools(self, context):
        llm = ScriptedLLM(responses=[
            AIMessage(content="", tool_calls=[{"name": "get_employee", "id": "c1",
                                              "args": {"employee_code": "EMP-003"}}]),
            AIMessage(content="Rohan Das West Bengal (WB) mein kaam karte hain."),
        ])  # fmt: skip
        reply = PayrollCopilot(context, llm=llm).ask("tell me something about Rohan", thread_id="l3")
        assert reply.route == "general" and reply.tools_used == ["get_employee"]
        assert reply.used_llm and "West Bengal" in reply.answer

    def test_general_agent_invented_number_falls_back_to_tool_output(self, context):
        llm = ScriptedLLM(responses=[
            AIMessage(content="", tool_calls=[{"name": "get_employee", "id": "c1",
                                              "args": {"employee_code": "EMP-003"}}]),
            AIMessage(content="Rohan ki salary ₹50,000 hai."),
        ])  # fmt: skip
        reply = PayrollCopilot(context, llm=llm).ask("tell me about Rohan", thread_id="l4")
        assert not reply.used_llm and "work_state: WB" in reply.answer
