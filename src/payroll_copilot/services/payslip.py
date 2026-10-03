"""Payslip JSON (stored in payroll_runs, shown as payslip_card) and payroll processing."""

import hashlib
import json
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PayrollCopilotError
from payroll_copilot.db.models import Employee, PayrollRun, PfCalculationAudit
from payroll_copilot.domain.enums import PayrollRunStatus
from payroll_copilot.repositories.payroll_repo import PayrollRepository
from payroll_copilot.services import audit
from payroll_copilot.services.payroll_service import PayrollCalculationService, PayrollView


class PayrollLockedError(PayrollCopilotError):
    """The month is locked (paid out) and cannot be processed again."""


def _lines(lines) -> list[dict[str, Any]]:
    return [{"head_code": b.head_code, "name": b.name + (" (arrear)" if b.is_arrear else ""),
             "amount": str(b.amount)} for b in lines]  # fmt: skip


def rule_versions(view: PayrollView) -> list[str]:
    results = (view.pf, view.esic, view.pt, view.lwf, view.tds)
    rules = [getattr(r, "rule", None) for r in results]
    return [f"{r.rule_code} v{r.version}" for r in rules if r is not None]


def payslip_dict(view: PayrollView, employee: Employee) -> dict[str, Any]:
    """JSON-safe payslip: money as strings, never floats."""
    s, salary = view.summary, view.salary.salary
    period = PayrollPeriod.parse(view.period)
    return {
        "employee_code": employee.employee_code,
        "employee_name": employee.full_name,
        "designation": employee.designation,
        "department": employee.department,
        "period": view.period,
        "period_label": period.label,
        "paid_days": str(salary.paid_days),
        "basis_days": str(salary.basis_days),
        "lop_days": str(view.salary.attendance.breakdown["lop_days"]),
        "earnings": _lines(s.earnings),
        "deductions": _lines(s.deductions),
        "employer_contributions": _lines(s.employer_contributions),
        "gross_earnings": str(s.gross_earnings),
        "total_deductions": str(s.total_deductions),
        "net_pay": str(s.net_pay),
        "monthly_ctc": str(s.monthly_ctc),
        "rule_versions": rule_versions(view),
        "pf_segments": view.pf.segments,
        "pf_details": view.pf.details,
        "overtime": view.adjustments.get("overtime", []),
        "adjustments": view.adjustments.get("adjustments", []),
        "warnings": view.warnings,
    }


def record_pf_audit(session: Session, employee: Employee, period: PayrollPeriod,
                    view: PayrollView, batch_id: str | None = None) -> PfCalculationAudit:
    """Spec section 26: everything needed to explain or debug one PF calculation."""
    pf = view.pf
    totals = {k: str(v) for k, v in pf.amounts.items()} if pf.applicable else {}
    totals.update({"pf_wage": str(pf.pf_wage), "eps_wage": str(pf.eps_wage),
                   "edli_wage": str(pf.edli_wage)} if pf.applicable else {})  # fmt: skip
    inputs = {"doj": employee.doj, "pf_applicable_date": employee.pf_applicable_date,
              "lwd": employee.lwd, "segments": pf.segments, "config": pf.details.get("config"),
              "lop": pf.details.get("lop_dates")}  # fmt: skip
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()
    row = PfCalculationAudit(
        employee_id=employee.id, period=str(period), batch_id=batch_id, doj=employee.doj,
        pf_applicable_date=employee.pf_applicable_date,
        status="OK" if pf.applicable else "NOT_APPLICABLE",
        rule_versions=pf.rule_versions, segments=pf.segments,
        totals={**totals, **({"reason": pf.reason} if pf.reason else {})},
        config=pf.details.get("config") or {}, input_hash=digest,
        lop_days=Decimal(view.salary.attendance.breakdown["lop_days"]),
        engine_version=pf.engine,
    )  # fmt: skip
    session.add(row)
    return row


def process_payroll(
    session: Session, employee: Employee, period: PayrollPeriod, run_by: Employee,
    request_id: str | None = None, batch_id: str | None = None,
) -> PayrollRun:  # fmt: skip
    """Calculate and store (or re-store) one employee's payroll for a month, audited."""
    repo = PayrollRepository(session)
    existing = repo.get(employee.id, period)
    if existing is not None and existing.status is PayrollRunStatus.LOCKED:
        raise PayrollLockedError(f"Payroll {period} for {employee.employee_code} is locked")
    view = PayrollCalculationService(session).calculate(employee.employee_code, period)
    slip = payslip_dict(view, employee)
    values = {
        "paid_days": view.salary.salary.paid_days, "lop_days": Decimal(slip["lop_days"]),
        "gross_earnings": view.summary.gross_earnings,
        "total_deductions": view.summary.total_deductions, "net_pay": view.summary.net_pay,
    }  # fmt: skip
    old = None
    if existing is None:
        run = PayrollRun(employee_id=employee.id, period=str(period), payslip=slip,
                         run_by_id=run_by.id, **values)  # fmt: skip
        session.add(run)
    else:
        run = existing
        old = {"version": run.version, "net_pay": run.net_pay, "lop_days": run.lop_days}
        run.version += 1
        run.payslip, run.run_by_id = slip, run_by.id
        for key, value in values.items():
            setattr(run, key, value)
    session.flush()
    record_pf_audit(session, employee, period, view, batch_id)
    audit.record(session, "payroll.run", actor=run_by, entity="payroll_run", entity_id=run.id,
                 old=old, new={"employee_code": employee.employee_code, "period": str(period),
                               "version": run.version, "net_pay": run.net_pay,
                               "lop_days": run.lop_days,
                               **({"batch_id": batch_id} if batch_id else {})},
                 request_id=request_id or batch_id)  # fmt: skip
    return run
