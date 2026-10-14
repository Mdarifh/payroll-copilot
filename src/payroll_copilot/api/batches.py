"""Bulk payroll endpoints (HR only). The chat starts a batch; the bulk LOP card uses these."""

from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from payroll_copilot.auth.deps import (
    get_session,
    require_approve,
    require_edit_attendance,
    require_hr,
    require_run_payroll,
)
from payroll_copilot.core.dates import parse_month
from payroll_copilot.db.models import Employee
from payroll_copilot.services import batch_service as svc
from payroll_copilot.services import payroll_runs


class StartBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    month: str = Field(max_length=30)
    department: str | None = Field(default=None, max_length=100)
    work_state: str | None = Field(default=None, max_length=50)


class LopItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply: bool | None = None
    lop_days: Decimal | None = Field(default=None, ge=0, le=31)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=500)


class RunBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["APPLY", "SKIP_ALL"] = "APPLY"
    reason: str | None = Field(default=None, max_length=500)


router = APIRouter(prefix="/batches", tags=["bulk payroll"])


def _wake(request: Request) -> None:
    worker = getattr(request.app.state, "worker", None)
    if worker is not None:
        worker.wake()


@router.post("", status_code=201, summary="Start an LOP review for many employees")
def start(body: StartBatchRequest, hr: Employee = Depends(require_run_payroll),
          session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    batch = svc.start_review(session, hr, parse_month(body.month), body.department,
                             body.work_state)  # fmt: skip
    return svc.summary(batch)


@router.get("/{batch_id}", summary="Batch summary and progress")
@router.get("/{batch_id}/status", summary="Progress (poll every 2 seconds)")
def status(batch_id: str, _: Employee = Depends(require_hr),
           session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.status(session, svc.get_batch(session, batch_id))


@router.get("/{batch_id}/lop-items", summary="Employees needing an LOP decision (paged)")
def lop_items(batch_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200),
              search: str | None = Query(None, max_length=100),
              _: Employee = Depends(require_hr),
              session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.lop_items(session, svc.get_batch(session, batch_id), page, page_size, search)


@router.patch("/{batch_id}/lop-items/{emp_id}", summary="Change one employee's LOP decision")
def update_lop(batch_id: str, emp_id: str, body: LopItemPatch,
               _: Employee = Depends(require_run_payroll),
               session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.update_lop_item(session, svc.get_batch(session, batch_id), emp_id, body.apply,
                               body.lop_days)  # fmt: skip


@router.post("/{batch_id}/run", status_code=202, summary="Apply LOP and queue payroll (once)")
def run(batch_id: str, body: RunBatchRequest, request: Request,
        hr: Employee = Depends(require_run_payroll),
        session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    batch = svc.request_run(session, batch_id, hr, body.mode, body.reason)
    session.commit()  # make it visible to the worker before waking it
    _wake(request)
    return svc.status(session, batch)


@router.post("/{batch_id}/retry-failed", status_code=202, summary="Process failed ones again")
def retry_failed(batch_id: str, request: Request, hr: Employee = Depends(require_run_payroll),
                 session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    batch = svc.retry_failed(session, batch_id, hr)
    session.commit()
    _wake(request)
    return svc.status(session, batch)


@router.get("/{batch_id}/report.csv", summary="Processed / LOP applied / failed per employee",
            response_class=Response)  # fmt: skip
def report(batch_id: str, _: Employee = Depends(require_hr),
           session: Session = Depends(get_session)) -> Response:  # fmt: skip
    batch = svc.get_batch(session, batch_id)
    name = f"payroll_batch_{batch.period}_{batch.batch_id[:8]}.csv"
    return Response(svc.report_csv(session, batch), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/{batch_id}/validate", summary="Run all payroll checks for the batch")
def validate(batch_id: str, hr: Employee = Depends(require_run_payroll),
             session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.validate(session, batch_id, hr)


@router.get("/{batch_id}/validation", summary="Errors / warnings / passed checks (paged)")
def validation(batch_id: str,
               severity: str | None = Query(None, pattern="^(?i)(error|warning|pass)$"),
               page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
               _: Employee = Depends(require_hr),
               session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.validation_results(session, svc.get_batch(session, batch_id), severity, page,
                                  page_size)  # fmt: skip


@router.post("/{batch_id}/approve", summary="Approve a validated payroll (maker-checker)")
def approve(batch_id: str, hr: Employee = Depends(require_approve),
            session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.status(session, svc.approve(session, batch_id, hr))


@router.post("/{batch_id}/finalize", summary="Lock the payslips of an approved payroll")
def finalize(batch_id: str, hr: Employee = Depends(require_approve),
             session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.status(session, svc.finalize(session, batch_id, hr))


@router.post("/{batch_id}/rollback", summary="Undo a calculated (not finalized) payroll")
def rollback(batch_id: str, body: RollbackRequest, hr: Employee = Depends(require_run_payroll),
             session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.status(session, svc.rollback(session, batch_id, hr, body.reason))


runs_router = APIRouter(prefix="/payroll-runs", tags=["bulk payroll"])


@runs_router.get("", summary="Payroll months with totals and status")
def months(_: Employee = Depends(require_hr),
           session: Session = Depends(get_session)) -> list[dict[str, Any]]:  # fmt: skip
    return payroll_runs.list_months(session)


@runs_router.get("/{month}", summary="One month: totals and every batch with its stage")
def month(month: str, hr: Employee = Depends(require_hr),
          session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return payroll_runs.month_detail(session, parse_month(month), hr)


class RecalculateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=500)
    department: str | None = Field(default=None, max_length=100)
    work_state: str | None = Field(default=None, max_length=50)


@runs_router.post("/{month}/recalculate", status_code=202,
                  summary="Re-run every processed, unlocked payslip of the month (queued)")
def recalculate(month: str, body: RecalculateRequest, request: Request,
                hr: Employee = Depends(require_run_payroll),
                session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    from payroll_copilot.services.payroll_actions import recalculate_month

    batch = recalculate_month(session, hr, parse_month(month), body.reason, body.department,
                              body.work_state)  # fmt: skip
    session.commit()  # visible to the worker before waking it
    _wake(request)
    return svc.status(session, batch)


class RecalculateEmployee(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=500)


@runs_router.post("/{month}/employees/{code}/recalculate",
                  summary="Re-run one processed, unlocked payslip (audit 'Fix')")
def recalculate_one(month: str, code: str, body: RecalculateEmployee, request: Request,
                    hr: Employee = Depends(require_run_payroll),
                    session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    from payroll_copilot.services.payroll_actions import recalculate_employee

    return recalculate_employee(session, hr, code, parse_month(month), body.reason,
                                request.state.request_id)  # fmt: skip


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=500)
    department: str | None = Field(default=None, max_length=100)
    work_state: str | None = Field(default=None, max_length=50)


@runs_router.get("/{month}/employees", summary="Employees of the month with their payslip")
def month_employees(month: str, search: str | None = Query(None, max_length=100),
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                    _: Employee = Depends(require_hr),
                    session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return payroll_runs.register(session, parse_month(month), search, page, page_size)


@runs_router.post("/{month}/attendance/create",
                  summary="Create missing attendance days for everyone (Sundays week off)")
def create_attendance(month: str, body: ReasonRequest,
                      hr: Employee = Depends(require_edit_attendance),
                      session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    from payroll_copilot.services.attendance_edit import create_for_month
    from payroll_copilot.services.payroll_source import normalise_state

    return create_for_month(session, hr, parse_month(month), body.reason, body.department,
                            normalise_state(body.work_state))  # fmt: skip


class RunSelectedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_codes: list[str] = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=5, max_length=500)
    # set LOP for some of the selected employees before the run ({code: days})
    lop_days: dict[str, Decimal] = Field(default_factory=dict, max_length=1000)


@runs_router.post("/{month}/run-selected", status_code=202,
                  summary="Run payroll only for the selected employees (queued)")
def run_selected(month: str, body: RunSelectedRequest, request: Request,
                 hr: Employee = Depends(require_run_payroll),
                 session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    from payroll_copilot.services.payroll_actions import run_selected as run

    batch = run(session, hr, parse_month(month), body.employee_codes, body.reason,
                body.lop_days)  # fmt: skip
    session.commit()  # visible to the worker before waking it
    _wake(request)
    return svc.status(session, batch)


@runs_router.post("/{month}/reopen", summary="Unlock a finalized month (reason required)")
def reopen(month: str, body: ReasonRequest, hr: Employee = Depends(require_approve),
           session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.reopen_month(session, parse_month(month), hr, body.reason)


@runs_router.get("/{month}/prechecks", summary="Checks before running payroll (read-only)")
def prechecks(month: str, department: str | None = Query(None, max_length=100),
              work_state: str | None = Query(None, max_length=50),
              _: Employee = Depends(require_hr),
              session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return payroll_runs.prechecks(session, parse_month(month), department, work_state)


@runs_router.get("/{month}/compliance", summary="Statutory rules, challan and audit health")
def compliance(month: str, _: Employee = Depends(require_hr),
               session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    from payroll_copilot.services.compliance import overview

    return overview(session, parse_month(month))


@runs_router.get("/{month}/challan", summary="EPF challan totals for a month")
def challan(month: str, _: Employee = Depends(require_hr),
            session: Session = Depends(get_session)) -> dict[str, Any]:  # fmt: skip
    return svc.pf_challan(session, parse_month(month))
