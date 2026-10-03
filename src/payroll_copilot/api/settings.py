"""Audit Timeline, users and roles, and company settings (read-only view + approval switch)."""

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from payroll_copilot.auth.deps import get_session, require_admin, require_audit, require_hr
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import Role
from payroll_copilot.services import audit_timeline, users

router = APIRouter(tags=["settings"])


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    login_enabled: bool | None = None
    reason: str = Field(min_length=5, max_length=500)


@router.get("/audit/timeline", summary="Who changed what and when (newest first)")
def timeline(employee: str | None = Query(None, max_length=32),
             action: str | None = Query(None, max_length=40, pattern=r"^[a-z_.]+$"),
             actor: str | None = Query(None, max_length=32),
             date_from: date | None = None, date_to: date | None = None,
             include_system: bool = False, page: int = Query(1, ge=1),
             page_size: int = Query(30, ge=1, le=200),
             _: Employee = Depends(require_audit),
             session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return audit_timeline.timeline(session, employee, action, actor, date_from, date_to,
                                   include_system, page, page_size)  # fmt: skip


@router.get("/settings/users", summary="Users, their roles and permissions (Admin)")
def list_users(search: str | None = Query(None, max_length=100),
               _: Employee = Depends(require_admin),
               session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return users.list_users(session, search)


@router.put("/settings/users/{code}", summary="Change a user's role or login (Admin)")
def update_user(code: str, body: UserUpdate, admin: Employee = Depends(require_admin),
                session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return users.update_user(session, admin, code, body.role.value if body.role else None,
                             body.login_enabled, body.reason)  # fmt: skip


@router.get("/settings/company", summary="Company payroll settings and policies (read-only)")
def company(_: Employee = Depends(require_hr),
            session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    from payroll_copilot.core.config import get_settings
    from payroll_copilot.services.company_config import get_config
    from payroll_copilot.services.pay_adjustments import overtime_policy

    settings = get_settings()
    return {
        "company_name": settings.company_name,
        "payroll": {k: v for k, v in get_config(session, date.today()).items()},
        "overtime_policy": overtime_policy().model_dump(mode="json"),
        "leave_entitlements": {k: str(v) for k, v in settings.leave_entitlements.items()},
        "llm": {"provider": settings.llm_provider,
                "model": getattr(settings, "ollama_model", None)},
    }  # fmt: skip
