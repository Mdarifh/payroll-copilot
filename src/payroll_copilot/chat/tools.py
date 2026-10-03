"""Tools the LLM may call. All numbers come from the payroll engine / database.

Security rules enforced here (not in the prompt):
* Employee tools have **no employee parameter**: they always act on the logged-in
  user, so "show EMP-102's payslip" from an employee cannot reach someone else's data.
* Each tool lists the roles allowed to use it; the LLM only sees the tools for the
  user's role, and ``run_tool`` checks the role again before running anything.
* Every tool call is written to the audit log.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.auth.permissions import STAFF_ROLES, Perm, roles_with
from payroll_copilot.core.dates import (
    FinancialYear,
    PayrollPeriod,
    parse_financial_year,
    parse_month,
)
from payroll_copilot.core.exceptions import NotFoundError, PayrollCopilotError, PeriodError
from payroll_copilot.db.models import Attendance, AuditLog, Employee, Leave, PayrollRun
from payroll_copilot.domain.enums import LeaveStatus, Role
from payroll_copilot.repositories.attendance_repo import AttendanceRepository
from payroll_copilot.repositories.payroll_repo import PayrollRepository
from payroll_copilot.services import audit
from payroll_copilot.services.leave_service import leave_balances
from payroll_copilot.services.tds_service import TDSService

ZERO = Decimal(0)
EVERYONE = frozenset(Role)  # self-service tools: every logged-in user
HR_ONLY = STAFF_ROLES  # payroll staff (any role that may view every employee's payroll)
RUN_ROLES = roles_with(Perm.RUN_PAYROLL)


class ToolError(PayrollCopilotError):
    """A problem the model should see and explain (bad month, not processed, ...)."""


@dataclass
class ToolContext:
    session: Session
    user: Employee
    today: date
    request_id: str | None = None
    # audit rows of read-only tool calls, written when the chat turn ends (so no write
    # lock is held on the database while the LLM is thinking)
    audit_rows: list[AuditLog] = field(default_factory=list)
    # saved conversation: follow-up context and the previous exchanges (after a restart)
    chat_context: dict[str, Any] = field(default_factory=dict)
    history: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ToolResult:
    content: dict[str, Any]  # what the LLM reads (JSON-safe, money as strings)
    cards: list[dict[str, Any]] = field(default_factory=list)  # what the UI renders


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema shown to the model
    args_model: type[BaseModel]
    roles: frozenset[Role]
    handler: Callable[[ToolContext, Any], ToolResult]


# ---------------------------------------------------------------------- arguments


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NoArgs(_Args):
    pass


class PayslipArgs(_Args):
    month: str | None = Field(default=None, max_length=30)

    @field_validator("month")
    @classmethod
    def _month(cls, v: str | None) -> str | None:
        return None if not v else _month_or_error(v)


class TaxSummaryArgs(_Args):
    financial_year: str | None = Field(default=None, max_length=20)

    @field_validator("financial_year")
    @classmethod
    def _fy(cls, v: str | None) -> str | None:
        return None if not v else _fy_or_error(v)


class AttendanceExceptionsArgs(_Args):
    emp_id: str = Field(min_length=2, max_length=32)
    month: str = Field(max_length=30)

    @field_validator("emp_id")
    @classmethod
    def _code(cls, v: str) -> str:
        return v.upper()

    @field_validator("month")
    @classmethod
    def _month(cls, v: str) -> str:
        return _month_or_error(v)


def _month_or_error(value: str) -> str:
    try:
        return str(parse_month(value))
    except PeriodError as exc:  # pydantic only turns ValueError into a validation error
        raise ValueError(str(exc)) from exc


def _fy_or_error(value: str) -> str:
    try:
        return str(parse_financial_year(value))
    except PeriodError as exc:
        raise ValueError(str(exc)) from exc


class StartPayrollReviewArgs(_Args):
    month: str = Field(max_length=30)
    department: str | None = Field(default=None, max_length=100)
    work_state: str | None = Field(default=None, max_length=50)

    @field_validator("month")
    @classmethod
    def _month(cls, v: str) -> str:
        return _month_or_error(v)


def _schema(properties: dict[str, Any], required: list[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}  # fmt: skip


_MONTH = {"type": "string", "description": "Payroll month as YYYY-MM, e.g. 2026-08"}

# ---------------------------------------------------------------------- handlers


def _payslip_summary(slip: dict[str, Any]) -> dict[str, Any]:
    return {
        "period": slip["period"], "paid_days": slip["paid_days"],
        "basis_days": slip["basis_days"], "lop_days": slip["lop_days"],
        "gross_earnings": slip["gross_earnings"], "total_deductions": slip["total_deductions"],
        "net_pay": slip["net_pay"],
        "earnings": {line["name"]: line["amount"] for line in slip["earnings"]},
        "deductions": {line["name"]: line["amount"] for line in slip["deductions"]},
    }  # fmt: skip


def get_payslip(ctx: ToolContext, args: PayslipArgs) -> ToolResult:
    repo = PayrollRepository(ctx.session)
    if args.month is None:
        run = ctx.session.scalar(
            select(PayrollRun).where(PayrollRun.employee_id == ctx.user.id)
            .order_by(PayrollRun.period.desc())
        )  # fmt: skip
        if run is None:
            raise ToolError("No payslip has been processed for you yet.")
    else:
        run = repo.get(ctx.user.id, PayrollPeriod.parse(args.month))
        if run is None:
            label = PayrollPeriod.parse(args.month).label
            raise ToolError(f"Payroll for {label} is not processed yet, so there is no payslip. "
                            "Payslips are available after HR runs payroll for the month.")
    slip = run.payslip
    card = {"type": "payslip_card", "payslip": slip,
            "download_url": f"/payslips/{run.period}/pdf"}  # fmt: skip
    return ToolResult(content=_payslip_summary(slip), cards=[card])


def get_leave_balance(ctx: ToolContext, args: NoArgs) -> ToolResult:
    fy = FinancialYear.from_date(ctx.today)
    balances = leave_balances(ctx.session, ctx.user, fy)
    pending = ctx.session.scalars(
        select(Leave).where(Leave.employee_id == ctx.user.id, Leave.status == LeaveStatus.PENDING)
        .order_by(Leave.from_date)
    )  # fmt: skip
    return ToolResult(content={
        "financial_year": str(fy),
        "balances": [b.as_dict() for b in balances],
        "pending_requests": [{"leave_type": p.leave_type.value, "from": p.from_date.isoformat(),
                              "to": p.to_date.isoformat(), "days": str(p.days)}
                             for p in pending],
    })  # fmt: skip


def _latest_month_with_attendance(ctx: ToolContext, fy: FinancialYear) -> PayrollPeriod:
    last = min(fy.end, ctx.today)
    day = ctx.session.scalar(
        select(Attendance.day).where(Attendance.employee_id == ctx.user.id,
                                     Attendance.day.between(fy.start, last))
        .order_by(Attendance.day.desc())
    )  # fmt: skip
    if day is None:
        raise ToolError(f"No attendance or salary data for FY {fy} yet.")
    return PayrollPeriod.from_date(day)


def get_tax_summary(ctx: ToolContext, args: TaxSummaryArgs) -> ToolResult:
    fy = (FinancialYear.parse(args.financial_year) if args.financial_year
          else FinancialYear.from_date(ctx.today))  # fmt: skip
    period = _latest_month_with_attendance(ctx, fy)
    tds = TDSService(ctx.session).calculate(ctx.user.employee_code, period)
    if not tds.applicable or not tds.configured or tds.annual is None:
        raise ToolError(f"Tax for FY {fy} cannot be calculated: {tds.reason}")
    months = [m for m in _fy_months(fy) if m <= period]
    deducted = sum(PayrollRepository(ctx.session).deduction_history(
        ctx.user.id, "TDS", months).values(), ZERO)  # fmt: skip
    a = tds.annual
    return ToolResult(content={
        "financial_year": str(fy), "regime": ctx.user.tax_regime.value,
        "based_on_month": str(period),
        "projected_annual_salary": str(tds.gross_taxable_salary),
        "standard_deduction": str(tds.standard_deduction),
        "professional_tax_deduction": str(tds.professional_tax),
        "other_deductions": {k: str(v) for k, v in tds.section_deductions.items()},
        "taxable_income": str(a.taxable_income), "tax_before_rebate": str(a.slab_tax),
        "rebate": str(a.rebate), "surcharge": str(a.surcharge), "cess": str(a.cess),
        "total_annual_tax": str(a.total_tax),
        "tds_deducted_in_processed_payslips": str(deducted),
        "monthly_tds_from_now": str(tds.monthly_tds),
        "note": "Projection from salary structure and processed payslips; final tax depends "
                "on actual pay and declarations.",
    })  # fmt: skip


def _fy_months(fy: FinancialYear) -> list[PayrollPeriod]:
    months, current = [], PayrollPeriod.from_date(fy.start)
    for _ in range(12):
        months.append(current)
        current = current.next()
    return months


def get_attendance_exceptions(ctx: ToolContext, args: AttendanceExceptionsArgs) -> ToolResult:
    employee = ctx.session.scalar(select(Employee).where(Employee.employee_code == args.emp_id))
    if employee is None:
        raise ToolError(f"Employee {args.emp_id} not found.")
    period = PayrollPeriod.parse(args.month)
    repo = AttendanceRepository(ctx.session)
    exceptions = repo.exceptions(employee.id, period)
    record = repo.lop_record(employee.id, period)
    run = PayrollRepository(ctx.session).get(employee.id, period)
    approved = ctx.session.scalars(select(Leave).where(
        Leave.employee_id == employee.id, Leave.status == LeaveStatus.APPROVED,
        Leave.from_date <= period.end, Leave.to_date >= period.start,
    ))  # fmt: skip
    return ToolResult(content={
        "employee_code": employee.employee_code, "employee_name": employee.full_name,
        "month": str(period),
        "unapproved_absences": [{"date": e.day.isoformat(), "weekday": e.day.strftime("%A"),
                                 "status": e.status.value, "days": str(e.days),
                                 "remarks": e.remarks} for e in exceptions],
        "total_unapproved_days": str(sum((e.days for e in exceptions), ZERO)),
        "lop_decision": None if record is None else {"lop_days": str(record.days),
                                                     "reason": record.reason},
        "approved_leaves": [{"leave_type": lv.leave_type.value, "from": lv.from_date.isoformat(),
                             "to": lv.to_date.isoformat(), "days": str(lv.days)}
                            for lv in approved],
        "payroll_processed": run is not None,
        "net_pay_if_processed": None if run is None else str(run.net_pay),
    })  # fmt: skip


def start_payroll_review(ctx: ToolContext, args: StartPayrollReviewArgs) -> ToolResult:
    """Create a bulk payroll batch. Its own transaction, committed now: the review must
    survive whatever happens to the rest of the chat turn. Only a summary goes back."""
    from sqlalchemy.orm import Session as _Session

    from payroll_copilot.services import batch_service

    with _Session(bind=ctx.session.get_bind(), expire_on_commit=False) as session:
        hr = session.get(Employee, ctx.user.id)
        batch = batch_service.start_review(session, hr, PayrollPeriod.parse(args.month),
                                           args.department, args.work_state)  # fmt: skip
        session.commit()
        info = batch_service.summary(batch)
    content = {**info, "next_step": "HR reviews LOP in the card, then presses the run button"
                                    if info["review_count"] else
                                    "No LOP review needed; HR can run payroll from the card"}
    return ToolResult(content=content, cards=[{"type": "bulk_lop_card", "batch_id":
                                               info["batch_id"], "summary": info}])


# ---------------------------------------------------------------------- explain / compare


_COMPONENT_ENUM = ["PF", "ESIC", "PT", "TDS", "LWF", "LOP", "GROSS", "NET"]


class ExplainArgs(_Args):
    component: str = Field(max_length=8)
    month: str | None = Field(default=None, max_length=30)

    @field_validator("component")
    @classmethod
    def _comp(cls, v: str) -> str:
        if v.upper() not in _COMPONENT_ENUM:
            raise ValueError(f"component must be one of {', '.join(_COMPONENT_ENUM)}")
        return v.upper()

    @field_validator("month")
    @classmethod
    def _month(cls, v: str | None) -> str | None:
        return None if not v else _month_or_error(v)


class EmployeeExplainArgs(ExplainArgs):
    emp_id: str = Field(min_length=2, max_length=60)


class CompareArgs(_Args):
    first_month: str = Field(max_length=30)
    second_month: str = Field(max_length=30)

    @field_validator("first_month", "second_month")
    @classmethod
    def _month(cls, v: str) -> str:
        return _month_or_error(v)


class EmployeeCompareArgs(CompareArgs):
    emp_id: str = Field(min_length=2, max_length=60)


class RulesArgs(_Args):
    topic: str = Field(max_length=8)
    month: str | None = Field(default=None, max_length=30)

    @field_validator("topic")
    @classmethod
    def _topic(cls, v: str) -> str:
        if v.upper() not in ("PF", "ESIC", "PT", "TDS", "LWF"):
            raise ValueError("topic must be PF, ESIC, PT, TDS or LWF")
        return v.upper()

    @field_validator("month")
    @classmethod
    def _month(cls, v: str | None) -> str | None:
        return None if not v else _month_or_error(v)


class FindEmployeeArgs(_Args):
    query: str = Field(min_length=2, max_length=60)


def _period_or_latest(ctx: ToolContext, employee: Employee, month: str | None) -> PayrollPeriod:
    if month:
        return PayrollPeriod.parse(month)
    latest = ctx.session.scalar(select(PayrollRun.period).where(
        PayrollRun.employee_id == employee.id).order_by(PayrollRun.period.desc()))
    return PayrollPeriod.parse(latest) if latest else PayrollPeriod.from_date(ctx.today)


def _explain(ctx: ToolContext, who: str | None, component: str, month: str | None) -> ToolResult:
    from payroll_copilot.services import payroll_insights as insights

    employee = insights.resolve_employee(ctx.session, ctx.user, who)
    x = insights.explain_component(ctx.session, ctx.user, employee.employee_code,
                                   _period_or_latest(ctx, employee, month), component)
    return ToolResult(content=x, cards=[{"type": "explain_card", **x}])


def explain_my_pay(ctx: ToolContext, args: ExplainArgs) -> ToolResult:
    return _explain(ctx, None, args.component, args.month)


def explain_employee_pay(ctx: ToolContext, args: EmployeeExplainArgs) -> ToolResult:
    return _explain(ctx, args.emp_id, args.component, args.month)


def _compare(ctx: ToolContext, who: str | None, first: str, second: str) -> ToolResult:
    from payroll_copilot.services import payroll_insights as insights

    c = insights.compare_months(ctx.session, ctx.user, who, PayrollPeriod.parse(first),
                                PayrollPeriod.parse(second))  # fmt: skip
    return ToolResult(content=c, cards=[{"type": "compare_card", **c}])


def compare_my_months(ctx: ToolContext, args: CompareArgs) -> ToolResult:
    return _compare(ctx, None, args.first_month, args.second_month)


def compare_employee_months(ctx: ToolContext, args: EmployeeCompareArgs) -> ToolResult:
    return _compare(ctx, args.emp_id, args.first_month, args.second_month)


def payroll_rules(ctx: ToolContext, args: RulesArgs) -> ToolResult:
    from payroll_copilot.services import payroll_insights as insights

    month = PayrollPeriod.parse(args.month) if args.month else PayrollPeriod.from_date(ctx.today)
    rules = insights.statutory_rules(ctx.session, ctx.user, args.topic, month)
    return ToolResult(content=rules, cards=[{"type": "rules_card", **rules}])


def find_employee(ctx: ToolContext, args: FindEmployeeArgs) -> ToolResult:
    from payroll_copilot.services import payroll_insights as insights

    found = insights.find_employees(ctx.session, ctx.user, args.query)
    if not found:
        raise ToolError(f"No employee matches '{args.query}'")
    return ToolResult(content={"matches": [
        {"employee_code": e.employee_code, "full_name": e.full_name,
         "department": e.department, "designation": e.designation} for e in found]})


_COMPONENT_PARAM = {"type": "string", "enum": _COMPONENT_ENUM,
                    "description": "PF, ESIC, PT, TDS, LWF, LOP (days), GROSS or NET"}
_EMP_PARAM = {"type": "string", "description": "Employee code (EMP-102) or name"}


TOOLS: list[ToolSpec] = [
    ToolSpec(
        "get_payslip",
        "Get the logged-in user's own processed payslip for a month: earnings, deductions, "
        "net pay, paid and LOP days. Omit month for the latest processed payslip.",
        _schema({"month": _MONTH}), PayslipArgs, EVERYONE, get_payslip,
    ),
    ToolSpec(
        "get_leave_balance",
        "Get the logged-in user's leave balance (CL, SL, EL) for the current financial year "
        "and their pending leave requests.",
        _schema({}), NoArgs, EVERYONE, get_leave_balance,
    ),
    ToolSpec(
        "get_tax_summary",
        "Get the logged-in user's projected income tax (TDS) for a financial year: projected "
        "salary, deductions, taxable income, annual tax, TDS deducted so far and monthly TDS.",
        _schema({"financial_year": {"type": "string",
                                    "description": "Indian financial year like 2026-27; "
                                                   "omit for the current one"}}),
        TaxSummaryArgs, EVERYONE, get_tax_summary,
    ),  # fmt: skip
    ToolSpec(
        "get_attendance_exceptions",
        "HR only. List an employee's absences without approved leave in a month (LOP "
        "candidates), the LOP already decided, approved leaves and whether payroll is "
        "processed.",
        _schema({"emp_id": {"type": "string", "description": "Employee code, e.g. EMP-102"},
                 "month": _MONTH}, ["emp_id", "month"]),
        AttendanceExceptionsArgs, HR_ONLY, get_attendance_exceptions,
    ),  # fmt: skip
    ToolSpec(
        "start_payroll_review",
        "HR only. Start payroll for a month for ALL employees, or only a department "
        "(e.g. Sales) or a state/office (e.g. Delhi). Finds absences without approved leave "
        "and opens an LOP review card; payroll runs only after HR confirms in the card. "
        "Returns counts only: total, ok_count, review_count.",
        _schema({"month": _MONTH,
                 "department": {"type": "string", "description": "Only this department"},
                 "work_state": {"type": "string",
                                "description": "Only this state or office, e.g. Delhi or DL"}},
                ["month"]),
        StartPayrollReviewArgs, RUN_ROLES, start_payroll_review,
    ),  # fmt: skip
    ToolSpec(
        "explain_my_pay",
        "Explain how one part of the logged-in user's pay was calculated for a month (amount, "
        "formula and steps from the payroll engine): PF, ESIC, PT, TDS, LWF, LOP, GROSS, NET.",
        _schema({"component": _COMPONENT_PARAM, "month": _MONTH}, ["component"]),
        ExplainArgs, EVERYONE, explain_my_pay,
    ),
    ToolSpec(
        "compare_my_months",
        "Compare the logged-in user's pay in two months and list the reasons found in the "
        "data (salary revision, LOP, overtime, one-off pay, arrears, rule changes).",
        _schema({"first_month": _MONTH, "second_month": _MONTH},
                ["first_month", "second_month"]),
        CompareArgs, EVERYONE, compare_my_months,
    ),
    ToolSpec(
        "payroll_rules",
        "The statutory rule values this payroll system uses for a month: PF rates and wage "
        "ceiling, ESIC rates and threshold, PT slabs, TDS regime, LWF (for the user's state).",
        _schema({"topic": {"type": "string", "enum": ["PF", "ESIC", "PT", "TDS", "LWF"]},
                 "month": _MONTH}, ["topic"]),
        RulesArgs, EVERYONE, payroll_rules,
    ),  # fmt: skip
    ToolSpec(
        "find_employee",
        "HR only. Find employees by name or code (returns codes, names, departments).",
        _schema({"query": {"type": "string", "description": "Name or code"}}, ["query"]),
        FindEmployeeArgs, HR_ONLY, find_employee,
    ),  # fmt: skip
    ToolSpec(
        "explain_employee_pay",
        "HR only. Explain one part of an employee's pay for a month (amount and engine steps).",
        _schema({"emp_id": _EMP_PARAM, "component": _COMPONENT_PARAM, "month": _MONTH},
                ["emp_id", "component"]),
        EmployeeExplainArgs, HR_ONLY, explain_employee_pay,
    ),
    ToolSpec(
        "compare_employee_months",
        "HR only. Compare an employee's pay in two months with the reasons found in the data.",
        _schema({"emp_id": _EMP_PARAM, "first_month": _MONTH, "second_month": _MONTH},
                ["emp_id", "first_month", "second_month"]),
        EmployeeCompareArgs, HR_ONLY, compare_employee_months,
    ),
]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def tools_for(role: Role) -> list[ToolSpec]:
    return [t for t in TOOLS if role in t.roles]


def run_tool(ctx: ToolContext, name: str, raw_args: dict[str, Any] | None) -> ToolResult:
    """Validate, authorise, run and audit one tool call. Errors raise ToolError."""
    spec = TOOLS_BY_NAME.get(name)
    allowed = spec is not None and ctx.user.role in spec.roles
    ctx.audit_rows.append(audit.entry(
        "chat.tool", actor=ctx.user, entity="tool", entity_id=name,
        new={"args": raw_args or {}, "allowed": allowed}, request_id=ctx.request_id,
    ))  # fmt: skip
    if spec is None:
        raise ToolError(f"Unknown tool {name}")
    if not allowed:
        raise ToolError(f"{name} is not allowed for the {ctx.user.role.value} role")
    try:
        args = spec.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: "
                             f"{e['msg'].removeprefix('Value error, ')}"
                             for e in exc.errors())  # fmt: skip
        raise ToolError(f"Invalid arguments for {name}: {problems}") from exc
    try:
        return spec.handler(ctx, args)
    except ToolError:
        raise
    except NotFoundError as exc:
        raise ToolError(str(exc)) from exc
    except PayrollCopilotError as exc:
        raise ToolError(f"{name} failed: {exc}") from exc

