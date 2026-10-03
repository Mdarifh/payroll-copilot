"""Leave applications (everyone) and leave approval (manager / attendance editors),
plus the old vs new tax regime comparison."""

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.auth.deps import current_user, get_session
from payroll_copilot.core.dates import PayrollPeriod, parse_month
from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.db.models import Employee
from payroll_copilot.services import leave_requests as svc
from payroll_copilot.services.tax_help import compare_regimes

router = APIRouter(tags=["leave and tax"])


class LeaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leave_type: str = Field(pattern="^(CL|SL|EL|LOP)$")
    from_date: date
    to_date: date | None = None
    reason: str = Field("", max_length=500)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field("", max_length=500)


@router.get("/leaves", summary="My leave balance and requests")
def my_leaves(user: Employee = Depends(current_user),
              session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.mine(session, user)


@router.post("/leaves/preview", summary="Check a leave application (nothing is saved)")
def preview(body: LeaveBody, user: Employee = Depends(current_user),
            session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.plan(session, user, body.leave_type, body.from_date, body.to_date)


@router.post("/leaves", summary="Apply for leave (waits for approval)")
def apply(body: LeaveBody, request: Request, user: Employee = Depends(current_user),
          session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.apply(session, user, body.leave_type, body.from_date, body.to_date, body.reason,
                     request_id=request.state.request_id)  # fmt: skip


@router.post("/leaves/{leave_id}/cancel", summary="Withdraw my pending request")
def cancel(leave_id: int, request: Request, user: Employee = Depends(current_user),
           session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.cancel(session, user, leave_id, request.state.request_id)


@router.get("/leaves/to-approve", summary="Pending requests I can decide")
def to_approve(user: Employee = Depends(current_user),
               session: Session = Depends(get_session)) -> list[dict[str, Any]]:  # fmt: skip
    return svc.to_decide(session, user)


@router.post("/leaves/{leave_id}/approve", summary="Approve (writes the leave into attendance)")
def approve(leave_id: int, body: Decision, request: Request,
            user: Employee = Depends(current_user),
            session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.decide(session, user, leave_id, True, body.note, request.state.request_id)


@router.post("/leaves/{leave_id}/reject", summary="Reject a leave request")
def reject(leave_id: int, body: Decision, request: Request,
           user: Employee = Depends(current_user),
           session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.decide(session, user, leave_id, False, body.note, request.state.request_id)


@router.get("/tax/compare", summary="Old vs new regime for the FY (what-if, nothing saved)")
def tax_compare(employee: str | None = Query(None, max_length=32),
                month: str | None = Query(None, max_length=30),
                c80: Decimal = Query(Decimal(0), alias="80C", ge=0, le=10_000_000),
                d80: Decimal = Query(Decimal(0), alias="80D", ge=0, le=10_000_000),
                nps: Decimal = Query(Decimal(0), alias="80CCD(1B)", ge=0, le=10_000_000),
                tta: Decimal = Query(Decimal(0), alias="80TTA", ge=0, le=10_000_000),
                user: Employee = Depends(current_user),
                session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    target = user
    if employee and employee.upper() != user.employee_code:
        target = session.scalar(select(Employee).where(Employee.employee_code == employee.upper()))
        if target is None:
            raise NotFoundError(f"Employee {employee} not found")
    period = parse_month(month) if month else PayrollPeriod.from_date(date.today())
    return compare_regimes(session, user, target, period,
                           {"80C": c80, "80D": d80, "80CCD(1B)": nps, "80TTA": tta})
