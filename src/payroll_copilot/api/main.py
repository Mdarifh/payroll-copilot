"""FastAPI application.

* Login with email + password -> JWT access token (``Authorization: Bearer ...``).
* Every endpoint except /health and login needs a valid token; HR-only endpoints
  check the role in the backend.
* ``POST /chat``: the LLM picks tools for the logged-in user; the reply is a list of
  typed messages (``text``, ``payslip_card``, ...) for the frontend to render.
* Errors are JSON ``{"error", "detail"}``: 401 not logged in, 403 wrong role,
  404 not found, 422 invalid input, 503 LLM / configuration unavailable.
"""

import logging
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from payroll_copilot import __version__
from payroll_copilot.api.actions import router as actions_router
from payroll_copilot.api.audits import router as audits_router
from payroll_copilot.api.batches import router as batch_router
from payroll_copilot.api.batches import runs_router
from payroll_copilot.api.directory import router as directory_router
from payroll_copilot.api.knowledge import router as knowledge_router
from payroll_copilot.api.leaves import router as leaves_router
from payroll_copilot.api.overview import router as overview_router
from payroll_copilot.api.payroll import router as payroll_router
from payroll_copilot.api.reports import router as reports_router
from payroll_copilot.api.schemas import (
    ChatRequest,
    ChatResponse,
    LoginRequest,
    TokenResponse,
    UserOut,
)
from payroll_copilot.api.settings import router as settings_router
from payroll_copilot.auth import service as auth_service
from payroll_copilot.auth.deps import current_user, get_session, require_hr
from payroll_copilot.chat.agent import ChatAgent
from payroll_copilot.chat.tools import ToolContext, tools_for
from payroll_copilot.core.config import Settings, get_settings
from payroll_copilot.core.dates import parse_month
from payroll_copilot.core.exceptions import (
    AuthError,
    ConfigurationError,
    NotFoundError,
    PayrollCopilotError,
    PermissionDeniedError,
)
from payroll_copilot.db.models import Employee
from payroll_copilot.db.session import get_engine, get_session_factory
from payroll_copilot.llm import get_chat_model
from payroll_copilot.llm.base import ChatModel, LLMUnavailableError
from payroll_copilot.repositories.payroll_repo import PayrollRepository
from payroll_copilot.services import audit
from payroll_copilot.services.action_center import ActionConflictError
from payroll_copilot.services.batch_service import BatchConflictError
from payroll_copilot.services.batch_worker import BatchWorker
from payroll_copilot.services.payslip_pdf import render_payslip_pdf

log = logging.getLogger(__name__)
_REQUEST_ID = re.compile(r"^[\w-]{8,64}$")


def user_out(e: Employee) -> UserOut:
    return UserOut(employee_id=e.id, employee_code=e.employee_code, full_name=e.full_name,
                   email=e.email, role=e.role.value, department=e.department,
                   designation=e.designation)  # fmt: skip


_DEFAULT_MODEL: Any = object()  # "build the model from settings"


def create_app(
    settings: Settings | None = None, engine: Engine | None = None,
    chat_model: ChatModel | None = _DEFAULT_MODEL, today: Callable[[], date] = date.today,
    worker: BatchWorker | None = _DEFAULT_MODEL,
) -> FastAPI:  # fmt: skip
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # the bulk payroll worker lives as long as the API; on start it resumes batches
        # that were running when the API stopped
        if app.state.worker is not None:
            app.state.worker.start()
        _sync_knowledge(app)
        yield
        if app.state.worker is not None:
            app.state.worker.stop()

    def _sync_knowledge(app: FastAPI) -> None:
        from payroll_copilot.services.knowledge import ingest

        try:
            with app.state.session_factory() as session:
                result = ingest(session)
                session.commit()
            log.info("knowledge base synced: %s", result)
        except Exception:  # noqa: BLE001 - the API must start even if a document is broken
            log.exception("knowledge base sync failed")

    app = FastAPI(title=f"{settings.app_name} API", version=__version__, lifespan=lifespan,
                  description="Payroll chatbot backend. Local demo data only.")  # fmt: skip
    app.state.session_factory = get_session_factory(engine or get_engine())
    if worker is _DEFAULT_MODEL:
        worker = (BatchWorker(app.state.session_factory) if settings.batch_worker_enabled
                  else None)  # fmt: skip
    app.state.worker = worker
    app.state.today = today
    app.state.chat_agent = None

    def chat_agent() -> ChatAgent:
        if app.state.chat_agent is None:
            model = get_chat_model(settings) if chat_model is _DEFAULT_MODEL else chat_model
            app.state.chat_agent = ChatAgent(model, max_steps=settings.llm_max_steps)
        return app.state.chat_agent
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])  # fmt: skip

    # ------------------------------------------------------------------ errors

    def _error(status: int, exc: Exception, headers: dict | None = None) -> JSONResponse:
        return JSONResponse(status_code=status, headers=headers,
                            content={"error": type(exc).__name__, "detail": str(exc)})

    @app.exception_handler(AuthError)
    async def _unauthorised(_: Request, exc: AuthError) -> JSONResponse:
        return _error(401, exc, {"WWW-Authenticate": "Bearer"})

    @app.exception_handler(PermissionDeniedError)
    async def _forbidden(_: Request, exc: PermissionDeniedError) -> JSONResponse:
        return _error(403, exc)

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error(404, exc)

    @app.exception_handler(LLMUnavailableError)
    async def _llm_down(_: Request, exc: LLMUnavailableError) -> JSONResponse:
        return _error(503, exc)

    @app.exception_handler(BatchConflictError)
    async def _conflict(_: Request, exc: BatchConflictError) -> JSONResponse:
        return _error(409, exc)

    @app.exception_handler(ActionConflictError)
    async def _action_conflict(_: Request, exc: ActionConflictError) -> JSONResponse:
        return _error(409, exc)

    @app.exception_handler(ConfigurationError)
    async def _config(_: Request, exc: ConfigurationError) -> JSONResponse:
        return _error(503, exc)

    @app.exception_handler(PayrollCopilotError)
    async def _domain(_: Request, exc: PayrollCopilotError) -> JSONResponse:
        return _error(422, exc)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID", "")
        request.state.request_id = rid if _REQUEST_ID.match(rid) else uuid4().hex[:16]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    # ------------------------------------------------------------------ health

    @app.get("/health", tags=["health"])
    def health() -> dict[str, Any]:
        try:
            with app.state.session_factory() as s:
                s.execute(text("SELECT 1"))
            database = "ok"
        except Exception as exc:  # noqa: BLE001 - health must report, not raise
            database = f"error: {exc.__class__.__name__}"
        return {"status": "ok" if database == "ok" else "degraded", "version": __version__,
                "database": database, "llm_provider": settings.llm_provider.value}  # fmt: skip

    # ------------------------------------------------------------------ auth

    @app.post("/auth/login", tags=["auth"], response_model=TokenResponse)
    def login(body: LoginRequest, request: Request,
              session: Session = Depends(get_session)) -> TokenResponse:  # fmt: skip
        user, token, expires = auth_service.login(session, body.email, body.password,
                                                  request.state.request_id)  # fmt: skip
        return TokenResponse(access_token=token, expires_at=expires, user=user_out(user))

    @app.post("/auth/token", tags=["auth"], summary="Login form (Swagger 'Authorize' button)")
    def token(request: Request, form: OAuth2PasswordRequestForm = Depends(),
              session: Session = Depends(get_session)) -> dict[str, str]:  # fmt: skip
        _, access, _ = auth_service.login(session, form.username, form.password,
                                          request.state.request_id)  # fmt: skip
        return {"access_token": access, "token_type": "bearer"}

    @app.get("/auth/me", tags=["auth"], response_model=UserOut)
    def me(user: Employee = Depends(current_user)) -> UserOut:
        return user_out(user)

    # ------------------------------------------------------------------ HR

    @app.get("/hr/employees", tags=["hr"], response_model=list[UserOut],
             summary="All employees (HR only)")  # fmt: skip
    def employees(_: Employee = Depends(require_hr),
                  session: Session = Depends(get_session)) -> list[UserOut]:  # fmt: skip
        rows = session.scalars(select(Employee).order_by(Employee.employee_code))
        return [user_out(e) for e in rows]

    # ------------------------------------------------------------------ chat

    @app.post("/chat", tags=["chat"], response_model=ChatResponse)
    def chat(body: ChatRequest, request: Request, user: Employee = Depends(current_user),
             session: Session = Depends(get_session)) -> ChatResponse:  # fmt: skip
        from payroll_copilot.services import chat_history

        conversation_id = body.conversation_id or uuid4().hex[:16]
        saved = chat_history.get(session, user, conversation_id)
        ctx = ToolContext(session=session, user=user, today=app.state.today(),
                          request_id=request.state.request_id,
                          chat_context=dict(saved.context) if saved else {},
                          history=chat_history.previous_turns(session, saved))  # fmt: skip
        reply = chat_agent().ask(ctx, body.message, conversation_id)
        chat = saved or chat_history.get_or_create(session, user, conversation_id, body.message)
        chat_history.append(session, chat, body.message, reply.messages, reply.tools_used)
        chat.context = ctx.chat_context
        session.add_all(ctx.audit_rows)
        audit.record(session, "chat.ask", actor=user, entity="conversation",
                     entity_id=reply.conversation_id,
                     new={"message": body.message[:300], "tools": reply.tools_used,
                          "model": reply.model, "warnings": reply.warnings},
                     request_id=request.state.request_id)  # fmt: skip
        return ChatResponse(**reply.__dict__)

    @app.delete("/chat/{conversation_id}", tags=["chat"], summary="Delete a conversation")
    def forget(conversation_id: str, user: Employee = Depends(current_user),
               session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
        from payroll_copilot.services import chat_history

        memory = chat_agent().store.clear(user.id, conversation_id)
        saved = chat_history.remove(session, user, conversation_id)
        return {"conversation_id": conversation_id, "cleared": memory or saved}

    @app.get("/chat/sessions", tags=["chat"], summary="Your saved conversations")
    def chat_sessions(user: Employee = Depends(current_user),
                      session: Session = Depends(get_session)) -> list[dict[str, Any]]:  # fmt: skip
        from payroll_copilot.services import chat_history

        return chat_history.list_sessions(session, user)

    @app.get("/chat/sessions/{conversation_id}", tags=["chat"],
             summary="One saved conversation with its messages")
    def chat_session(conversation_id: str, user: Employee = Depends(current_user),
                     session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
        from payroll_copilot.services import chat_history

        return chat_history.messages(session, user, conversation_id)

    @app.get("/chat/tools", tags=["chat"], summary="Tools the assistant may use for you")
    def my_tools(user: Employee = Depends(current_user)) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in tools_for(user.role)]  # fmt: skip

    @app.get("/payslips/{period}", tags=["payslips"], summary="Your processed payslip")
    def payslip(period: str, user: Employee = Depends(current_user),
                session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
        run = PayrollRepository(session).get(user.id, parse_month(period))
        if run is None:
            raise NotFoundError(f"No processed payslip for {period}")
        return run.payslip

    @app.get("/payslips/{period}/pdf", tags=["payslips"], summary="Your payslip as PDF",
             response_class=Response)  # fmt: skip
    def payslip_pdf(period: str, request: Request, user: Employee = Depends(current_user),
                    session: Session = Depends(get_session)) -> Response:  # fmt: skip
        month = parse_month(period)
        run = PayrollRepository(session).get(user.id, month)
        if run is None:
            raise NotFoundError(f"No processed payslip for {period}")
        audit.record(session, "payslip.download", actor=user, entity="payroll_run",
                     entity_id=run.id, request_id=request.state.request_id)  # fmt: skip
        name = f"payslip_{user.employee_code}_{month}.pdf"
        return Response(render_payslip_pdf(run.payslip), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    app.include_router(batch_router)
    app.include_router(runs_router)
    app.include_router(directory_router)
    app.include_router(overview_router)
    app.include_router(payroll_router)
    app.include_router(actions_router)
    app.include_router(audits_router)
    app.include_router(knowledge_router)
    app.include_router(settings_router)
    app.include_router(reports_router)
    app.include_router(leaves_router)
    return app
