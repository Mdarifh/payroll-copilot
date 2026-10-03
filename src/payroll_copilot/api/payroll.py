"""What-if payroll preview (HR). Read-only: the change is rolled back, never saved."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from payroll_copilot.auth.deps import get_session, require_hr
from payroll_copilot.core.dates import parse_month
from payroll_copilot.db.models import Employee
from payroll_copilot.services.simulation import PayrollChange, simulate

router = APIRouter(prefix="/payroll", tags=["payroll"])


class SimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_code: str = Field(max_length=32)
    month: str = Field(max_length=30)
    change: PayrollChange


@router.post("/simulate", summary="Before / after / difference of a proposed change (not saved)")
def simulate_change(body: SimulateRequest, hr: Employee = Depends(require_hr),
                    session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return simulate(session, hr, body.employee_code, parse_month(body.month), body.change)


class ApplyRequest(SimulateRequest):
    preview_id: str = Field(min_length=8, max_length=64)
    source: Literal["ui", "chat"] = "ui"


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=500)


@router.post("/apply", summary="Submit a previewed change (applied now, or sent for approval)")
def apply_change(body: ApplyRequest, request: Request, hr: Employee = Depends(require_hr),
                 session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    from payroll_copilot.services.action_center import submit

    return submit(session, hr, body.employee_code, parse_month(body.month), body.change,
                  body.preview_id, body.source, request.state.request_id)  # fmt: skip


@router.post("/entries/{kind}/{entry_id}/cancel", summary="Cancel an overtime / one-off entry")
def cancel_entry(kind: Literal["overtime", "adjustment"], entry_id: int, body: CancelRequest,
                 request: Request, hr: Employee = Depends(require_hr),
                 session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    from payroll_copilot.services.payroll_actions import cancel_entry as cancel

    return cancel(session, hr, kind, entry_id, body.reason, request.state.request_id)
