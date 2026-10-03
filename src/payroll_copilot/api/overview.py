"""Overview dashboard endpoint (read-only)."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from payroll_copilot.auth.deps import current_user, get_session
from payroll_copilot.db.models import Employee
from payroll_copilot.services.overview import overview

router = APIRouter(tags=["overview"])


@router.get("/overview", summary="Dashboard numbers (company for HR, personal otherwise)")
def get_overview(user: Employee = Depends(current_user),
                 session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return overview(session, user)
