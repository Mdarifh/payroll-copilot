"""FastAPI application.

* Every endpoint except /health requires ``X-API-Key`` when API_KEY is set
  (always set in prod; optional in dev for local use).
* Engine operations are the same functions the LangChain tools use; domain errors
  become 404 (not found), 422 (invalid data / rules) or 503 (configuration).
* Money in responses is a string (never a float).
"""

import logging
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func, select, text

from payroll_copilot import __version__
from payroll_copilot.api.schemas import (
    ApprovalDecision,
    ApprovalRequest,
    ChatRequest,
    EmployeePeriodRequest,
    EmployeeRequest,
    OperationResponse,
    PeriodRequest,
    TDSRequest,
)
from payroll_copilot.audit.context import audit_context
from payroll_copilot.core.config import Settings, get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import (
    ConfigurationError,
    NotFoundError,
    PayrollCopilotError,
    PeriodError,
)
from payroll_copilot.db.models import Attendance, Employee
from payroll_copilot.rag.service import KnowledgeBase
from payroll_copilot.services.approval_service import (
    ApprovalError,
    ApprovalPermissionError,
    ApprovalService,
)
from payroll_copilot.tools.registry import SessionProvider, ToolContext, build_tools, operations
from payroll_copilot.tools.schemas import PolicySearchArgs

log = logging.getLogger(__name__)

_USER = re.compile(r"^[\w.@-]{1,64}$")
_REQUEST_ID = re.compile(r"^[\w-]{8,64}$")
_NOT_AUDITED = {"/health", "/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"}


def _period(value: str) -> str:
    try:
        return str(PayrollPeriod.parse(value))
    except PeriodError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_app(
    settings: Settings | None = None,
    sessions: SessionProvider | None = None,
    knowledge_base: Callable[[], KnowledgeBase] | None = None,
    copilot_factory: Callable[[ToolContext], Any] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    ctx = ToolContext(**({"sessions": sessions} if sessions else {}),
                      knowledge_base=knowledge_base)  # fmt: skip
    ops = operations(ctx)
    app = FastAPI(
        title="AI Payroll Copilot API",
        version=__version__,
        description="Deterministic payroll engine, QA and policy search. Local data only.",
    )
    app.state.copilot = None

    if settings.api_key is None:
        log.warning("API_KEY not set: API is unauthenticated (allowed only outside prod)")

    def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        expected = settings.api_key.get_secret_value() if settings.api_key else None
        if expected is None:
            return
        if x_api_key is None or not secrets.compare_digest(x_api_key, expected):
            raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")

    # ------------------------------------------------------------------ errors

    def _error(status: int, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status,
                            content={"error": type(exc).__name__, "detail": str(exc)})

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error(404, exc)

    @app.exception_handler(ApprovalPermissionError)
    async def _forbidden(_: Request, exc: ApprovalPermissionError) -> JSONResponse:
        return _error(403, exc)

    @app.exception_handler(ApprovalError)
    async def _conflict(_: Request, exc: ApprovalError) -> JSONResponse:
        return _error(409, exc)

    @app.exception_handler(ConfigurationError)
    async def _config(_: Request, exc: ConfigurationError) -> JSONResponse:
        return _error(503, exc)

    @app.exception_handler(PayrollCopilotError)
    async def _domain(_: Request, exc: PayrollCopilotError) -> JSONResponse:
        return _error(422, exc)

    @app.exception_handler(ValidationError)
    async def _invalid(_: Request, exc: ValidationError) -> JSONResponse:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg'].removeprefix('Value error, ')}"
            for e in exc.errors()
        )
        return JSONResponse(status_code=422, content={"error": "ValidationError", "detail": detail})

    def run(name: str, **kwargs: Any) -> OperationResponse:
        content, artifact = ops[name](**kwargs)
        return OperationResponse(text=content, data=artifact)

    # ------------------------------------------------------------------ audit middleware

    def verified(user: str | None, token: str | None) -> bool:
        """A well-formed X-User; with USER_TOKENS set it also needs that user's own token."""
        if not user or not _USER.match(user):
            return False
        if not settings.user_tokens:
            return True
        expected = settings.user_tokens.get(user)
        return bool(expected and token
                    and secrets.compare_digest(token, expected.get_secret_value()))  # fmt: skip

    @app.middleware("http")
    async def audit_requests(request: Request, call_next):
        user = request.headers.get("X-User", "")
        token = request.headers.get("X-User-Token")
        actor = f"api:{user}" if verified(user, token) else "api:anonymous"
        request_id = request.headers.get("X-Request-ID", "")
        request_id = request_id if _REQUEST_ID.match(request_id) else None
        start = time.perf_counter()
        with audit_context(actor=actor, request_id=request_id) as rid:
            response = await call_next(request)
            if request.url.path not in _NOT_AUDITED:
                await run_in_threadpool(
                    ctx.audit.log, "api.request", entity="endpoint", entity_id=request.url.path,
                    details={"method": request.method, "path": request.url.path,
                             "query": dict(request.query_params), "status": response.status_code,
                             "duration_ms": round((time.perf_counter() - start) * 1000)},
                )  # fmt: skip
            response.headers["X-Request-ID"] = rid
        return response

    # ------------------------------------------------------------------ health

    @app.get("/health", tags=["health"])
    def health() -> dict[str, Any]:
        latest_period = None
        try:
            with ctx.sessions() as s:
                s.execute(text("SELECT 1"))
                latest_period = s.scalar(select(func.max(Attendance.period)))
            database = "ok"
        except Exception as exc:  # noqa: BLE001 - health must report, not raise
            database = f"error: {exc.__class__.__name__}"
        return {"status": "ok" if database == "ok" else "degraded", "version": __version__,
                "database": database, "llm_provider": settings.llm_provider.value,
                "latest_period": latest_period}  # fmt: skip

    api = APIRouter(dependencies=[Depends(require_api_key)])

    # ------------------------------------------------------------------ employees

    @api.get("/employees", tags=["employees"])
    def list_employees() -> list[dict[str, Any]]:
        with ctx.sessions() as s:
            rows = s.scalars(select(Employee).order_by(Employee.employee_code))
            return [{"employee_code": e.employee_code, "full_name": e.full_name,
                     "work_state": e.work_state, "status": e.status.value,
                     "doj": e.doj.isoformat(), "lwd": e.lwd.isoformat() if e.lwd else None}
                    for e in rows]  # fmt: skip

    @api.get("/employees/{employee_code}", tags=["employees"])
    def get_employee(employee_code: str) -> OperationResponse:
        return run("get_employee", employee_code=EmployeeRequest(employee_code=employee_code)
                   .employee_code)  # fmt: skip

    def _employee_period(name: str, employee_code: str, period: str) -> OperationResponse:
        req = EmployeePeriodRequest(employee_code=employee_code, period=_period(period))
        return run(name, employee_code=req.employee_code, period=req.period)

    @api.get("/employees/{employee_code}/attendance", tags=["employees"])
    def attendance(employee_code: str, period: str = Query(..., examples=["2026-09"])):
        return _employee_period("get_attendance", employee_code, period)

    @api.get("/employees/{employee_code}/salary-structure", tags=["employees"])
    def salary_structure(employee_code: str, period: str = Query(..., examples=["2026-09"])):
        return _employee_period("get_salary_structure", employee_code, period)

    @api.get("/employees/{employee_code}/payroll", tags=["employees"],
             summary="Actual payslip from the payroll system")  # fmt: skip
    def actual_payroll(employee_code: str, period: str = Query(..., examples=["2026-09"])):
        result = _employee_period("get_payroll", employee_code, period)
        if result.data.get("actual") is None:
            raise HTTPException(status_code=404, detail=result.text)
        return result

    @api.get("/employees/{employee_code}/payslip", tags=["employees"],
             summary="Expected payslip calculated by the engine")  # fmt: skip
    def expected_payslip(employee_code: str, period: str = Query(..., examples=["2026-09"])):
        return _employee_period("get_payslip", employee_code, period)

    # ------------------------------------------------------------------ calculations

    def _calc(name: str) -> Callable[[EmployeePeriodRequest], OperationResponse]:
        def endpoint(body: EmployeePeriodRequest) -> OperationResponse:
            return run(name, employee_code=body.employee_code, period=body.period)

        endpoint.__name__ = name
        return endpoint

    for path, name in (("salary", "calculate_salary"), ("pf", "calculate_pf"),
                       ("esic", "calculate_esic"), ("pt", "calculate_pt"),
                       ("arrear", "calculate_arrear"), ("payroll", "get_payslip"),
                       ("pf-rules", "compare_pf_rules")):  # fmt: skip
        api.add_api_route(f"/calculate/{path}", _calc(name), methods=["POST"],
                          tags=["calculations"], response_model=OperationResponse)  # fmt: skip

    @api.post("/calculate/tds", tags=["calculations"])
    def calculate_tds(body: TDSRequest) -> OperationResponse:
        return run("calculate_tds", employee_code=body.employee_code, period=body.period,
                   override_amount=body.override_amount, override_reason=body.override_reason)

    @api.post("/calculate/fnf", tags=["calculations"])
    def calculate_fnf(body: EmployeeRequest) -> OperationResponse:
        return run("calculate_fnf", employee_code=body.employee_code)

    # ------------------------------------------------------------------ validation / QA

    @api.post("/validate/employee", tags=["validation"])
    def validate_employee(body: EmployeePeriodRequest) -> OperationResponse:
        return run("compare_expected_actual", employee_code=body.employee_code,
                   period=body.period)  # fmt: skip

    @api.post("/validate/payroll", tags=["validation"],
              summary="QA report for a payroll month (not saved)")  # fmt: skip
    def validate_payroll(body: PeriodRequest) -> OperationResponse:
        return run("run_payroll_qa", period=body.period)

    @api.get("/qa/runs", tags=["validation"], summary="Saved QA runs")
    def qa_runs(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
        from payroll_copilot.services.qa_service import QAService

        with ctx.sessions() as s:
            return [{"id": r.id, "period": r.period, "status": r.status.value,
                     "employees_checked": r.employees_checked, "findings": r.issues_found,
                     "created_at": r.created_at.isoformat(), "summary": r.summary}
                    for r in QAService(s).history(limit)]  # fmt: skip

    @api.get("/qa/runs/{run_id}", tags=["validation"])
    def qa_run(run_id: int) -> dict[str, Any]:
        from payroll_copilot.services.qa_service import QAService

        with ctx.sessions() as s:
            run_row, issues = QAService(s).load(run_id)
            return {
                "id": run_row.id, "period": run_row.period, "summary": run_row.summary,
                "issues": [{"component": i.component, "category": i.category,
                            "status": i.status.value, "root_cause": i.root_cause,
                            "expected": None if i.expected is None else str(i.expected),
                            "actual": None if i.actual is None else str(i.actual),
                            "difference": None if i.difference is None else str(i.difference),
                            "primary": i.trace.get("primary", False)} for i in issues],
            }  # fmt: skip

    # ------------------------------------------------------------------ knowledge / chat

    @api.post("/kb/search", tags=["knowledge"])
    def kb_search(body: PolicySearchArgs) -> dict[str, Any]:
        period = PayrollPeriod.parse(body.period) if body.period else None
        chunks = ctx.kb.search(body.query, module=body.module, period=period, state=body.state,
                               k=body.k)  # fmt: skip
        return {"results": [{"citation": c.citation, "text": c.text, "score": round(c.score, 5),
                             "metadata": c.metadata} for c in chunks]}  # fmt: skip

    def copilot():
        if app.state.copilot is None:
            if copilot_factory is not None:
                app.state.copilot = copilot_factory(ctx)
            else:
                from payroll_copilot.agent.copilot import PayrollCopilot

                app.state.copilot = PayrollCopilot(ctx, use_configured_llm=settings.llm_enabled)
        return app.state.copilot

    def _thread(thread_id: str) -> str:
        if not re.fullmatch(r"[\w.-]{1,100}", thread_id):
            raise HTTPException(status_code=422, detail="invalid thread id")
        return thread_id

    @api.get("/chat/{thread_id}", tags=["chat"], summary="Stored conversation and memory")
    def chat_history(thread_id: str) -> dict[str, Any]:
        bot = copilot()
        return {"thread_id": _thread(thread_id), "memory": bot.memory(thread_id),
                "messages": bot.history(thread_id)}  # fmt: skip

    @api.delete("/chat/{thread_id}", tags=["chat"], summary="Forget a conversation")
    def chat_clear(thread_id: str) -> dict[str, Any]:
        copilot().clear(_thread(thread_id))
        return {"thread_id": thread_id, "cleared": True}

    # ------------------------------------------------------------------ approvals

    def _approvals(session) -> ApprovalService:
        return ApprovalService(session, settings.approvers)

    def _user(x_user: str | None, x_user_token: str | None) -> str:
        if not x_user or not _USER.match(x_user):
            raise HTTPException(status_code=401, detail="X-User header required for approvals")
        if not verified(x_user, x_user_token):
            raise HTTPException(status_code=401, detail="Missing or invalid X-User-Token")
        return x_user

    @api.post("/approvals", tags=["approvals"], status_code=201,
              summary="Request a write action (needs another user's approval)")  # fmt: skip
    def request_approval(
        body: ApprovalRequest, x_user: str | None = Header(default=None),
        x_user_token: str | None = Header(default=None),
    ) -> dict[str, Any]:  # fmt: skip
        user = _user(x_user, x_user_token)
        with ctx.write_sessions() as s:
            view = _approvals(s).request(body.action_type, body.payload,
                                              requested_by=f"api:{user}", reason=body.reason)
        return view.model_dump()

    @api.get("/approvals", tags=["approvals"])
    def list_approvals(status: str | None = None,
                       limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:  # fmt: skip
        with ctx.sessions() as s:
            return [v.model_dump() for v in _approvals(s).list(status, limit)]

    @api.get("/approvals/{approval_id}", tags=["approvals"])
    def get_approval(approval_id: int) -> dict[str, Any]:
        with ctx.sessions() as s:
            return _approvals(s).get(approval_id).model_dump()

    @api.post("/approvals/{approval_id}/approve", tags=["approvals"])
    def approve(approval_id: int, body: ApprovalDecision,
                x_user: str | None = Header(default=None),
                x_user_token: str | None = Header(default=None)) -> dict[str, Any]:  # fmt: skip
        user = _user(x_user, x_user_token)
        with ctx.write_sessions() as s:
            return _approvals(s).approve(approval_id, user, body.comment).model_dump()

    @api.post("/approvals/{approval_id}/reject", tags=["approvals"])
    def reject(approval_id: int, body: ApprovalDecision,
               x_user: str | None = Header(default=None),
               x_user_token: str | None = Header(default=None)) -> dict[str, Any]:  # fmt: skip
        user = _user(x_user, x_user_token)
        with ctx.write_sessions() as s:
            return _approvals(s).reject(approval_id, user, body.comment or "").model_dump()

    @api.get("/audit", tags=["audit"], summary="Audit trail (newest first)")
    def audit(action: str | None = None, entity_id: str | None = None,
              request_id: str | None = None,
              limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:  # fmt: skip
        return ctx.audit.search(action=action, entity_id=entity_id, request_id=request_id,
                                limit=limit)  # fmt: skip

    @api.post("/chat", tags=["chat"])
    def chat(body: ChatRequest) -> dict[str, Any]:
        reply = copilot().ask(body.message, thread_id=body.thread_id)
        return {"answer": reply.answer, "intent": reply.intent, "route": reply.route,
                "entities": reply.entities, "tools_used": reply.tools_used,
                "artifacts": reply.artifacts, "sources": reply.sources,
                "warnings": reply.warnings, "checks": reply.checks, "used_llm": reply.used_llm,
                "request_id": reply.request_id, "pending_action": reply.pending_action}

    @api.get("/tools", tags=["chat"], summary="Tools available to the agent")
    def list_tools() -> list[dict[str, Any]]:
        return [{"name": t.name, "access": t.metadata["access"], "description": t.description,
                 "args": list(t.args)} for t in build_tools(ctx)]  # fmt: skip

    app.include_router(api)
    return app
