"""Payroll QA workflow as a LangGraph subgraph (deterministic, read-only).

scope → validate → categorize → summarize → END

* scope: employees employed in the period and whether an actual run exists
* validate: expected vs actual for every employee (Phase 11 engine)
* categorize: group mismatches into primary findings with effects and severity
* summarize: totals, statutory impact and the readable report
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from payroll_copilot.calculations.qa import QAReport, build_report
from payroll_copilot.core.dates import PayrollPeriod


class QAState(TypedDict, total=False):
    period: str
    run_exists: bool
    scope: list[str]
    validation: Any  # PeriodValidation
    report: QAReport
    text: str


def build_qa_graph(validation_service_factory, payroll_repo_factory):
    """Factories receive nothing and return services bound to an open session."""

    def scope(state: QAState) -> dict:
        period = PayrollPeriod.parse(state["period"])
        repo = payroll_repo_factory()
        return {"run_exists": repo.run(period) is not None}

    def validate(state: QAState) -> dict:
        result = validation_service_factory().validate_period(PayrollPeriod.parse(state["period"]))
        return {"validation": result, "scope": [e.employee_code for e in result.employees]}

    def categorize(state: QAState) -> dict:
        return {"report": build_report(state["period"], state["validation"].employees)}

    def summarize(state: QAState) -> dict:
        text = state["report"].to_text()
        if not state["run_exists"]:
            text = (f"No actual payroll run found for {state['period']}; every employee is "
                    f"reported as missing a payslip.\n\n{text}")  # fmt: skip
        return {"text": text}

    graph = StateGraph(QAState)
    for name, node in (("scope", scope), ("validate", validate), ("categorize", categorize),
                       ("summarize", summarize)):  # fmt: skip
        graph.add_node(name, node)
    graph.add_edge(START, "scope")
    graph.add_edge("scope", "validate")
    graph.add_edge("validate", "categorize")
    graph.add_edge("categorize", "summarize")
    graph.add_edge("summarize", END)
    return graph.compile()
