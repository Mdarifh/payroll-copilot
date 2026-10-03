"""Action Center endpoints (HR). Approving needs a different HR user than the requester."""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from payroll_copilot.auth.deps import get_session, require_admin, require_approve, require_hr
from payroll_copilot.db.models import Employee
from payroll_copilot.services import action_center as svc

router = APIRouter(prefix="/actions", tags=["action center"])


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(min_length=8, max_length=64)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=5, max_length=500)


class ApprovalSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    reason: str = Field(min_length=5, max_length=500)


@router.get("/settings", summary="Do payroll changes need a second HR user's approval?")
def settings(hr: Employee = Depends(require_hr),
             session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.approval_setting(session, hr)


@router.put("/settings", summary="Turn approval of payroll changes on / off (audited)")
def update_settings(body: ApprovalSetting, hr: Employee = Depends(require_admin),
                    session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.set_approval(session, hr, body.enabled, body.reason)


@router.get("", summary="Actions by status (paged) with counts")
def actions(status: str | None = Query(None, max_length=20),
            search: str | None = Query(None, max_length=100),
            page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
            _: Employee = Depends(require_hr),
            session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.list_actions(session, status, page, page_size, search)


@router.get("/{action_id}", summary="One action")
def action(action_id: str, _: Employee = Depends(require_hr),
           session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.detail(session, svc._get(session, action_id))


@router.get("/{action_id}/preview", summary="Fresh preview of a pending action (to approve)")
def preview(action_id: str, hr: Employee = Depends(require_hr),
            session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.fresh_preview(session, hr, action_id)


@router.post("/{action_id}/approve", summary="Approve and apply (maker-checker)")
def approve(action_id: str, body: ApproveRequest, request: Request,
            hr: Employee = Depends(require_approve),
            session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.approve(session, hr, action_id, body.preview_id, request.state.request_id)


class UndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(min_length=8, max_length=64)
    reason: str = Field(min_length=5, max_length=500)


@router.get("/{action_id}/undo-preview", summary="What undoing an applied change would do")
def undo_preview(action_id: str, hr: Employee = Depends(require_hr),
                 session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.undo_preview(session, hr, action_id)


@router.post("/{action_id}/undo", summary="Undo an applied change (as a new action)")
def undo(action_id: str, body: UndoRequest, request: Request,
         hr: Employee = Depends(require_hr),
         session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.undo(session, hr, action_id, body.preview_id, body.reason,
                    request.state.request_id)  # fmt: skip


@router.post("/{action_id}/reject", summary="Reject (or withdraw your own request)")
def reject(action_id: str, body: RejectRequest, hr: Employee = Depends(require_hr),
           session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.reject(session, hr, action_id, body.note)
