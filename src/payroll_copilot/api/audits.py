"""AI Audit endpoints (HR). Audits run in the background; poll GET /audits/{id}."""

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.auth.deps import get_session, require_hr
from payroll_copilot.core.dates import parse_month
from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.db.models import AuditRun, Employee
from payroll_copilot.services import payroll_audit as svc

router = APIRouter(prefix="/audits", tags=["ai audit"])


class StartAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    month: str = Field(max_length=30)
    department: str | None = Field(default=None, max_length=100)
    work_state: str | None = Field(default=None, max_length=50)


@router.post("", status_code=202, summary="Start an AI audit of a month (runs in background)")
def start(body: StartAudit, request: Request, tasks: BackgroundTasks,
          hr: Employee = Depends(require_hr),
          session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    row = svc.start(session, hr, parse_month(body.month), body.department, body.work_state)
    session.commit()  # visible to the background task
    tasks.add_task(svc.execute, request.app.state.session_factory, row.public_id)
    return svc.run_dict(session, row, full=False)


@router.get("", summary="Recent audit runs")
def runs(_: Employee = Depends(require_hr),
         session: Session = Depends(get_session)) -> list[dict[str, Any]]:  # fmt: skip
    return svc.list_runs(session)


@router.get("/latest", summary="Latest completed audit (optionally for a month)")
def latest(month: str | None = Query(None, max_length=30), _: Employee = Depends(require_hr),
           session: Session = Depends(get_session)) -> dict[str, Any] | None:  # fmt: skip
    row = svc.latest(session, parse_month(month) if month else None)
    return svc.run_dict(session, row) if row else None


@router.get("/{audit_id}", summary="One audit with findings, anomalies and health")
def one(audit_id: str, _: Employee = Depends(require_hr),
        session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    row = session.scalar(select(AuditRun).where(AuditRun.public_id == audit_id))
    if row is None:
        raise NotFoundError(f"Audit {audit_id} not found")
    return svc.run_dict(session, row)
