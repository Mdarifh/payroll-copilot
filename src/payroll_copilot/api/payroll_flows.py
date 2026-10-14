"""Payroll runs started from the chat: a LangGraph workflow paused for HR's decisions."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from payroll_copilot.auth.deps import require_run_payroll
from payroll_copilot.core.dates import parse_month
from payroll_copilot.db.models import Employee
from payroll_copilot.services.payroll_flow import PayrollFlow

router = APIRouter(prefix="/payroll-flows", tags=["payroll runs (chat)"])


class StartFlowRequest(BaseModel):
    month: str = Field(max_length=30)
    department: str | None = Field(default=None, max_length=100)
    work_state: str | None = Field(default=None, max_length=50)


class ResumeFlowRequest(BaseModel):
    action: Literal["approve", "recheck", "cancel", "continue", "retry"]
    mode: Literal["APPLY", "SKIP_ALL"] | None = None
    reason: str | None = Field(default=None, max_length=500)


def payroll_flow(request: Request) -> PayrollFlow:
    return request.app.state.payroll_flow()


@router.post("", status_code=201, summary="Start a payroll run (pre-checks, then LOP review)")
def start(body: StartFlowRequest, hr: Employee = Depends(require_run_payroll),
          flow: PayrollFlow = Depends(payroll_flow)) -> dict[str, Any]:  # fmt: skip
    return flow.start(hr, parse_month(body.month), body.department, body.work_state)


@router.get("/{flow_id}", summary="Current step of a payroll run")
def get(flow_id: str, _: Employee = Depends(require_run_payroll),
        flow: PayrollFlow = Depends(payroll_flow)) -> dict[str, Any]:  # fmt: skip
    return flow.card(flow_id)


@router.post("/{flow_id}/resume", summary="Approve, re-check, cancel, or continue after the run")
def resume(flow_id: str, body: ResumeFlowRequest, hr: Employee = Depends(require_run_payroll),
           flow: PayrollFlow = Depends(payroll_flow)) -> dict[str, Any]:  # fmt: skip
    return flow.resume(hr, flow_id, body.model_dump(exclude_none=True))
