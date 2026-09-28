"""LangChain tools over the deterministic payroll services.

Every tool:
* has a validated Pydantic argument schema;
* is read-only (``TOOL_ACCESS`` = "read"; write tools arrive with the approval
  workflow and will be marked "write");
* returns ``(content, artifact)``: short text for the LLM plus structured JSON
  (values, traces, rule versions) for the UI / graph state;
* turns domain errors into a readable "ERROR: ..." result instead of raising, so
  the agent can explain or ask for missing input.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import wraps
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from sqlalchemy.orm import Session

from payroll_copilot.audit.logger import AuditLogger, rule_versions
from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PayrollCopilotError
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.session import read_scope, session_scope
from payroll_copilot.rag.service import KnowledgeBase
from payroll_copilot.tools.schemas import (
    EmployeeArgs,
    EmployeePeriodArgs,
    PeriodArgs,
    PolicySearchArgs,
    TDSArgs,
)

ToolOutput = tuple[str, dict[str, Any]]
SessionProvider = Callable[[], AbstractContextManager[Session]]


class ToolContext:
    """What the tools need at runtime: DB sessions, the knowledge base and the audit log."""

    def __init__(
        self,
        sessions: SessionProvider = read_scope,  # tools never commit
        knowledge_base: Callable[[], KnowledgeBase] | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.sessions = sessions
        # Only the approval workflow writes (to create/decide approval requests).
        self.write_sessions: SessionProvider = session_scope if sessions is read_scope else sessions
        self._kb_factory = knowledge_base or KnowledgeBase
        self._kb: KnowledgeBase | None = None
        if audit is None:
            enabled = get_settings().audit_enabled
            # read_scope never commits, so production audit uses its own committing scope
            audit = AuditLogger(None if sessions is read_scope else sessions, enabled=enabled)
        self.audit = audit

    @property
    def kb(self) -> KnowledgeBase:
        if self._kb is None:
            self._kb = self._kb_factory()
        return self._kb


def _mask(value: str | None) -> str | None:
    """Show only the last 4 characters of identifiers such as PAN/UAN."""
    if not value:
        return value
    return "*" * max(len(value) - 4, 0) + value[-4:]


def _safe(func: Callable[..., ToolOutput]) -> Callable[..., ToolOutput]:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> ToolOutput:
        try:
            return func(*args, **kwargs)
        except PayrollCopilotError as exc:
            return f"ERROR: {exc}", {"ok": False, "error": str(exc)}

    return wrapper


def _audited(ctx: ToolContext, name: str, func: Callable[..., ToolOutput]):
    """Record every agent tool call: arguments, outcome and rule versions used."""

    @wraps(func)
    def wrapper(**kwargs: Any) -> ToolOutput:
        content, artifact = func(**kwargs)
        ok = bool(artifact.get("ok", True))
        ctx.audit.log(
            "tool.call", tool_name=name,
            entity="employee" if "employee_code" in kwargs else "period",
            entity_id=kwargs.get("employee_code") or kwargs.get("period"),
            details={"args": {k: str(v) for k, v in kwargs.items() if v is not None},
                     "ok": ok, "error": artifact.get("error"),
                     "rule_versions": rule_versions(artifact)},
        )  # fmt: skip
        return content, artifact

    return wrapper


def _artifact(**data: Any) -> dict[str, Any]:
    return {"ok": True, **data}


def _validation_message(error: Exception) -> str:
    errors = getattr(error, "errors", None)
    if callable(errors):
        parts = []
        for item in errors():
            field = ".".join(str(p) for p in item.get("loc", ())) or "arguments"
            parts.append(f"{field}: {item.get('msg', '').removeprefix('Value error, ')}")
        return "ERROR: invalid arguments - " + "; ".join(parts)
    return f"ERROR: invalid arguments - {error}"


# ---------------------------------------------------------------------- tools


def _make_tools(ctx: ToolContext) -> list[tuple[str, str, type, Callable[..., ToolOutput]]]:
    from payroll_copilot.repositories.employee_repo import EmployeeRepository
    from payroll_copilot.repositories.payroll_repo import PayrollRepository
    from payroll_copilot.services.attendance_service import AttendanceService
    from payroll_copilot.services.esic_service import ESICService, render_esic
    from payroll_copilot.services.expected import ExpectedPayrollService
    from payroll_copilot.services.fnf_service import FnFService
    from payroll_copilot.services.pf_service import PFService, render_comparison, render_pf
    from payroll_copilot.services.pt_service import PTService, render_pt
    from payroll_copilot.services.salary_service import SalaryService
    from payroll_copilot.services.salary_structure_service import SalaryStructureService
    from payroll_copilot.services.tds_service import TDSService, render_tds
    from payroll_copilot.services.validation_service import ValidationService

    P = PayrollPeriod.parse

    def get_employee(employee_code: str) -> ToolOutput:
        with ctx.sessions() as s:
            e = EmployeeRepository(s).get_by_code(employee_code)
            data = {
                "employee_code": e.employee_code, "full_name": e.full_name,
                "gender": e.gender.value, "dob": e.dob.isoformat(), "doj": e.doj.isoformat(),
                "lwd": e.lwd.isoformat() if e.lwd else None, "status": e.status.value,
                "work_state": e.work_state, "department": e.department,
                "designation": e.designation, "pf_applicable": e.pf_applicable,
                "uan": _mask(e.uan), "eps_eligible": e.eps_eligible,
                "pf_wage_restricted": e.pf_wage_restricted, "esic_applicable": e.esic_applicable,
                "pt_applicable": e.pt_applicable, "lwf_applicable": e.lwf_applicable,
                "tax_regime": e.tax_regime.value, "pan": _mask(e.pan),
            }  # fmt: skip
        text = "\n".join(f"{k}: {v}" for k, v in data.items())
        return text, _artifact(employee=data)

    def get_salary_structure(employee_code: str, period: str) -> ToolOutput:
        with ctx.sessions() as s:
            view = SalaryStructureService(s).get_salary_structure(employee_code, P(period))
        return view.to_text(), _artifact(structure=view.model_dump(mode="json"))

    def get_attendance(employee_code: str, period: str) -> ToolOutput:
        with ctx.sessions() as s:
            view = AttendanceService(s).get_attendance(employee_code, P(period))
        return view.to_text(), _artifact(attendance=view.model_dump(mode="json"),
                                         traces=[view.paid_days.to_json_dict()])  # fmt: skip

    def get_payroll(employee_code: str, period: str) -> ToolOutput:
        with ctx.sessions() as s:
            employee = EmployeeRepository(s).get_by_code(employee_code)
            slip = PayrollRepository(s).actual_payslip(employee, P(period))
        if slip is None:
            return (f"No actual payslip for {employee_code} in {period}",
                    _artifact(actual=None))  # fmt: skip
        lines = [f"Actual payslip {employee_code} {period}: paid days {slip.paid_days}, "
                 f"gross {format_inr(slip.gross_earnings)}, net {format_inr(slip.net_pay)}"]
        lines += [f"  earning {h}: {format_inr(a)}" for h, a in slip.earnings.items()]
        lines += [f"  deduction {h}: {format_inr(a)}" for h, a in slip.deductions.items()]
        lines += [f"  employer {h}: {format_inr(a)}" for h, a in slip.employer.items()]  # fmt: skip
        return "\n".join(lines), _artifact(actual=slip.model_dump(mode="json"))

    def get_payslip(employee_code: str, period: str) -> ToolOutput:
        with ctx.sessions() as s:
            employee = EmployeeRepository(s).get_by_code(employee_code)
            view = ExpectedPayrollService(s).payslip(employee, P(period))
        return view.to_text(), _artifact(payslip=view.summary.model_dump(mode="json"),
                                         warnings=view.warnings)  # fmt: skip

    def calculate_salary(employee_code: str, period: str) -> ToolOutput:
        with ctx.sessions() as s:
            view = SalaryService(s).calculate(employee_code, P(period))
        return view.to_text(), _artifact(gross=str(view.salary.gross_earnings),
                                         reduction={k: str(v) for k, v in
                                                    view.salary.reduction.items()},
                                         traces=[view.salary.trace.to_json_dict(),
                                                 view.attendance.to_json_dict()])  # fmt: skip

    def calculate_pf(employee_code: str, period: str) -> ToolOutput:
        with ctx.sessions() as s:
            result = PFService(s).calculate(employee_code, P(period))
        text = render_pf(result, employee_code, period)
        if result.trace:
            text += "\n\n" + result.trace.to_text()
        return text, _artifact(pf=result.model_dump(mode="json", exclude={"components", "trace"}),
                               traces=[t.to_json_dict() for t in result.components.values()])

    def compare_pf_rules(employee_code: str, period: str) -> ToolOutput:
        with ctx.sessions() as s:
            rows = PFService(s).compare_rule_versions(employee_code, P(period))
        return render_comparison(rows, employee_code, period), _artifact(
            rows=[{"rule": r.rule_label, "applicable": r.is_applicable_rule,
                   "amounts": {k: str(v) for k, v in r.result.amounts.items()}} for r in rows])

    def calculate_esic(employee_code: str, period: str) -> ToolOutput:
        with ctx.sessions() as s:
            result = ESICService(s).calculate(employee_code, P(period))
        text = render_esic(result, employee_code, period)
        if result.trace:
            text += "\n\n" + result.trace.to_text()
        return text, _artifact(esic=result.model_dump(mode="json", exclude={"components", "trace"}),
                               traces=[result.trace.to_json_dict()] if result.trace else [])

    def calculate_pt(employee_code: str, period: str) -> ToolOutput:
        with ctx.sessions() as s:
            result = PTService(s).calculate(employee_code, P(period))
        text = render_pt(result, employee_code, period)
        if result.trace:
            text += "\n\n" + result.trace.to_text()
        return text, _artifact(pt=result.model_dump(mode="json", exclude={"trace"}),
                               traces=[result.trace.to_json_dict()] if result.trace else [])

    def calculate_tds(employee_code: str, period: str, override_amount=None,
                      override_reason: str | None = None) -> ToolOutput:  # fmt: skip
        with ctx.sessions() as s:
            result = TDSService(s).calculate(employee_code, P(period), override_amount,
                                             override_reason)  # fmt: skip
        text = render_tds(result, employee_code, period)
        return text, _artifact(tds=result.model_dump(mode="json", exclude={"trace"}),
                               traces=[result.trace.to_json_dict()] if result.trace else [])

    def calculate_arrear(employee_code: str, period: str) -> ToolOutput:
        with ctx.sessions() as s:
            view = SalaryService(s).calculate(employee_code, P(period))
        arrears = view.arrears
        if arrears is None:
            return f"No salary arrears paid to {employee_code} in {period}", _artifact(arrears=None)
        return arrears.trace.to_text(), _artifact(arrears=arrears.model_dump(mode="json"),
                                                  traces=[arrears.trace.to_json_dict()])

    def calculate_fnf(employee_code: str) -> ToolOutput:
        with ctx.sessions() as s:
            view = FnFService(s).calculate(employee_code)
        return view.to_text(), _artifact(net_settlement=str(view.net_settlement),
                                         fnf=view.fnf.model_dump(mode="json"))  # fmt: skip

    def compare_expected_actual(employee_code: str, period: str) -> ToolOutput:
        with ctx.sessions() as s:
            result = ValidationService(s).validate_employee(employee_code, P(period))
        text = result.to_text(only_issues=True) if result.issues else (
            f"{employee_code} {period}: all components match expected values")  # fmt: skip
        return text, _artifact(validation=result.model_dump(mode="json"))

    def validate_payroll(period: str) -> ToolOutput:
        with ctx.sessions() as s:
            result = ValidationService(s).validate_period(P(period))
        return result.to_text(), _artifact(
            period=result.period, by_group=result.by_group, by_issue_type=result.by_issue_type,
            employees=[{"employee_code": e.employee_code, "issues": len(e.issues),
                        "note": e.note} for e in result.employees],
        )  # fmt: skip

    def run_payroll_qa(period: str) -> ToolOutput:
        from payroll_copilot.services.qa_service import QAService

        with ctx.sessions() as s:
            report, text, _ = QAService(s).run(P(period))
        return text, _artifact(summary=report.summary,
                               findings=[f.model_dump(mode="json") for f in report.findings])

    def search_payroll_policy(query: str, module=None, period: str | None = None,
                              state: str | None = None, k: int = 4) -> ToolOutput:  # fmt: skip
        answer = ctx.kb.ask(query, module=module, period=P(period) if period else None,
                            state=state, k=k)  # fmt: skip
        text = answer.answer + ("\n\nSources:\n" + "\n".join(answer.sources)
                                if answer.sources else "")  # fmt: skip
        return text, _artifact(found=answer.found, sources=answer.sources)

    E, EP = EmployeeArgs, EmployeePeriodArgs
    return [
        ("get_employee", "Employee master data (DOJ, LWD, state, PF/ESIC/PT flags, tax regime). "
         "PAN/UAN are masked.", E, get_employee),
        ("get_salary_structure", "Salary structure for a month: earnings, wage bases, template "
         "check and which statutory deductions/employer contributions apply.", EP,
         get_salary_structure),
        ("get_attendance", "Attendance summary and paid days (with LOP) for a month, with the "
         "paid-days calculation trace.", EP, get_attendance),
        ("get_payroll", "The ACTUAL payslip stored from the payroll system for a month.", EP,
         get_payroll),
        ("get_payslip", "The EXPECTED payslip for a month calculated by the engine: earnings, "
         "deductions, net pay, employer contributions, CTC.", EP, get_payslip),
        ("calculate_salary", "Prorated earnings for a month and why gross differs from the "
         "full-month salary (LOP, joining/leaving, revision, arrears).", EP, calculate_salary),
        ("calculate_pf", "Employee PF, Employer EPF, EPS, EDLI and EPF admin with the rule "
         "version and full trace.", EP, calculate_pf),
        ("compare_pf_rules", "Same earnings under every PF rule version (e.g. old ₹15,000 vs "
         "new ₹25,000 ceiling).", EP, compare_pf_rules),
        ("calculate_esic", "ESIC coverage and employee/employer ESIC with trace.", EP,
         calculate_esic),
        ("calculate_pt", "Professional Tax for the employee's work state with slab and trace.",
         EP, calculate_pt),
        ("calculate_tds", "Projected annual tax and monthly TDS with trace; optional what-if "
         "override that is not saved.", TDSArgs, calculate_tds),
        ("calculate_arrear", "Salary arrears paid in a month with month-wise PF impact.", EP,
         calculate_arrear),
        ("calculate_fnf", "Full & final settlement of a separated employee.", E, calculate_fnf),
        ("compare_expected_actual", "Expected vs actual payslip for one employee: differences "
         "with root causes.", EP, compare_expected_actual),
        ("validate_payroll", "Component-level expected vs actual for every employee of a "
         "month's actual payroll run.", PeriodArgs, validate_payroll),
        ("run_payroll_qa", "Payroll QA report for a month: grouped findings with severity, "
         "root cause, impact and recommendations (not saved).", PeriodArgs, run_payroll_qa),
        ("search_payroll_policy", "Search the payroll policy / rule knowledge base; returns "
         "passages with sources. Use period and module filters.", PolicySearchArgs,
         search_payroll_policy),
    ]  # fmt: skip


TOOL_ACCESS: dict[str, str] = {
    name: "read"
    for name in (
        "get_employee", "get_salary_structure", "get_attendance", "get_payroll", "get_payslip",
        "calculate_salary", "calculate_pf", "compare_pf_rules", "calculate_esic", "calculate_pt",
        "calculate_tds", "calculate_arrear", "calculate_fnf", "compare_expected_actual",
        "validate_payroll", "run_payroll_qa", "search_payroll_policy",
    )
}  # fmt: skip

_SUFFIX = (" Numbers come from the deterministic payroll engine; never recompute them. "
           "Read-only.")  # fmt: skip


def operations(ctx: ToolContext | None = None) -> dict[str, Callable[..., ToolOutput]]:
    """The raw operations behind the tools. They raise domain errors (used by the API,
    which maps them to HTTP status codes) instead of returning "ERROR: ..." text."""
    return {name: func for name, _, _, func in _make_tools(ctx or ToolContext())}


def build_tools(ctx: ToolContext | None = None) -> list[BaseTool]:
    ctx = ctx or ToolContext()
    tools = []
    for name, description, schema, func in _make_tools(ctx):
        tools.append(
            StructuredTool.from_function(
                func=_audited(ctx, name, _safe(func)),
                name=name,
                description=description + _SUFFIX,
                args_schema=schema,
                response_format="content_and_artifact",
                handle_validation_error=_validation_message,
                metadata={"access": TOOL_ACCESS[name]},
            )
        )
    return tools
