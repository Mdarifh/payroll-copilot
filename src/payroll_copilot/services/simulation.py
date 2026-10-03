"""What-if payroll: the impact of a proposed change, without saving anything.

    before = payroll engine on today's data
    after  = the change applied by the SAME validated services (set_lop, add_overtime,
             add_adjustment, salary revision) inside a database savepoint, then the
             payroll engine again - and the savepoint is rolled back.

So a preview can never differ from what "apply" will do, and the LLM never does the
arithmetic. Validation errors of the change are returned exactly as apply would raise them.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.structure import compute_structure_breakup
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.db.models import Employee, SalaryStructure
from payroll_copilot.services.pay_adjustments import (
    AdjustmentError,
    _not_locked,
    add_adjustment,
    add_overtime,
)
from payroll_copilot.services.payroll_service import PayrollCalculationService, PayrollView
from payroll_copilot.services.salary_config import salary_heads, structure_templates

ZERO = Decimal(0)
STATUTORY = (("EE_PF", "PF"), ("EE_ESIC", "ESIC"), ("PT", "PT"), ("TDS", "TDS"),
             ("LWF_EE", "LWF"))  # fmt: skip


class OvertimeChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_date: date
    hours: Decimal


class AdjustmentChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    head_code: str
    amount: Decimal
    label: str | None = None


class SalaryChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthly_gross: Decimal
    effective_from: date
    template_code: str | None = None  # default: the employee's current template


class AttendanceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    status: str
    remarks: str | None = None


class CancelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str  # overtime | adjustment
    id: int


class PayrollChange(BaseModel):
    """A proposed change. Every part is optional; at least one must be given."""

    model_config = ConfigDict(extra="forbid")
    lop_days: Decimal | None = None
    overtime: list[OvertimeChange] = Field(default_factory=list)
    adjustments: list[AdjustmentChange] = Field(default_factory=list)
    salary: SalaryChange | None = None
    attendance: list[AttendanceChange] = Field(default_factory=list)
    fill_month: bool = False  # create the month's missing attendance days
    cancel_entries: list[CancelEntry] = Field(default_factory=list)  # undo of added entries
    reason: str = "What-if preview"

    def empty(self) -> bool:
        return (self.lop_days is None and not self.overtime and not self.adjustments
                and self.salary is None and not self.attendance and not self.fill_month
                and not self.cancel_entries)


def revise_salary(session: Session, actor: Employee, employee: Employee,
                  change: SalaryChange) -> str:  # fmt: skip
    """New structure version from ``effective_from`` using a template (engine breakup)."""
    from payroll_copilot.services import audit

    if change.monthly_gross <= 0:
        raise AdjustmentError("Monthly gross must be positive")
    if change.effective_from < employee.doj:
        raise AdjustmentError(f"Effective date is before DOJ {employee.doj}")
    _not_locked(session, employee, PayrollPeriod.from_date(change.effective_from))
    rows = list(session.scalars(select(SalaryStructure).where(
        SalaryStructure.employee_id == employee.id,
        SalaryStructure.effective_from <= change.effective_from)
        .order_by(SalaryStructure.effective_from.desc())))  # fmt: skip
    current = [r for r in rows if r.effective_from == rows[0].effective_from]
    code = change.template_code or next((r.template_code for r in current if r.template_code),
                                        None)  # fmt: skip
    template = structure_templates().get(code or "")
    if template is None:
        raise AdjustmentError("Choose a salary structure template for the revision")
    breakup = compute_structure_breakup(template, change.monthly_gross, salary_heads(),
                                        employee_code=employee.employee_code)  # fmt: skip
    later = session.scalar(select(SalaryStructure.effective_from).where(
        SalaryStructure.employee_id == employee.id,
        SalaryStructure.effective_from > change.effective_from)
        .order_by(SalaryStructure.effective_from))  # fmt: skip
    if later is not None:
        raise AdjustmentError(f"A later salary revision already starts on {later}")
    for r in current:  # close the version in force
        if r.effective_from == change.effective_from:
            session.delete(r)
        else:
            r.effective_to = change.effective_from - timedelta(days=1)
    session.flush()
    session.add_all(SalaryStructure(employee_id=employee.id, effective_from=change.effective_from,
                                    head_code=h, monthly_amount=a, template_code=template.code)
                    for h, a in breakup.breakdown.items())  # fmt: skip
    session.flush()
    audit.record(session, "salary.revise", actor=actor, entity="employee", entity_id=employee.id,
                 old={"components": {r.head_code: r.monthly_amount for r in current}},
                 new={"effective_from": change.effective_from, "template": template.code,
                      "monthly_gross": change.monthly_gross,
                      "components": dict(breakup.breakdown)})  # fmt: skip
    return template.code


def apply_change(session: Session, actor: Employee, employee: Employee, period: PayrollPeriod,
                 change: PayrollChange, request_id: str | None = None) -> None:  # fmt: skip
    """Apply ``change`` with the validated services (caller decides commit or rollback)."""
    from payroll_copilot.services.lop_service import set_lop

    if change.empty():
        raise AdjustmentError("Nothing to change")
    if change.salary is not None:
        revise_salary(session, actor, employee, change.salary)
    if change.attendance or change.fill_month:  # attendance first: LOP is checked against it
        from payroll_copilot.services.attendance_edit import update_days

        update_days(session, actor, employee, period,
                    [a.model_dump() for a in change.attendance], change.fill_month,
                    change.reason, request_id=request_id)  # fmt: skip
    if change.lop_days is not None:
        set_lop(session, actor, employee, period, change.lop_days, change.reason, request_id)
    for ot in change.overtime:
        add_overtime(session, actor, employee, ot.work_date, ot.hours, change.reason, period,
                     request_id)  # fmt: skip
    for adj in change.adjustments:
        add_adjustment(session, actor, employee, period, adj.kind, adj.head_code, adj.amount,
                       change.reason, adj.label, request_id)  # fmt: skip
    if change.cancel_entries:
        from payroll_copilot.db.models import OvertimeEntry, PayAdjustment
        from payroll_copilot.services.pay_adjustments import cancel

        for item in change.cancel_entries:
            model = OvertimeEntry if item.kind == "overtime" else PayAdjustment
            row = session.get(model, item.id)
            if row is None or row.employee_id != employee.id:
                raise AdjustmentError(f"{item.kind} entry {item.id} not found for this employee")
            cancel(session, actor, row, change.reason)


def _numbers(view: PayrollView) -> dict[str, Any]:
    s = view.summary
    lines = {ln.head_code: ln.amount for ln in s.deductions}
    return {
        "paid_days": view.salary.salary.paid_days,
        "lop_days": view.salary.attendance.breakdown["lop_days"],
        "gross": s.gross_earnings,
        **{label: lines.get(code, ZERO) for code, label in STATUTORY},
        "deductions": s.total_deductions,
        "net": s.net_pay,
        "earnings": {ln.head_code: ln.amount for ln in s.earnings},
        "employer": {ln.head_code: ln.amount for ln in s.employer_contributions},
    }


def _compare(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    heads = salary_heads()
    keys = ["paid_days", "lop_days", "gross", *(label for _, label in STATUTORY),
            "deductions", "net"]  # fmt: skip
    labels = {"paid_days": "Paid days", "lop_days": "LOP days", "gross": "Gross earnings",
              "deductions": "Total deductions", "net": "Net pay"}  # fmt: skip
    rows = [{"key": k, "label": labels.get(k, k), "unit": "DAYS" if k.endswith("days") else "INR",
             "before": str(before[k]), "after": str(after[k]),
             "difference": str(after[k] - before[k])} for k in keys]  # fmt: skip
    for group, label in (("earnings", "Earning"), ("employer", "Employer")):
        for code in dict.fromkeys([*before[group], *after[group]]):
            b, a = before[group].get(code, ZERO), after[group].get(code, ZERO)
            if a != b:
                rows.append({"key": f"{group}:{code}", "label": f"{label}: {heads[code].name}",
                             "unit": "INR", "before": str(b), "after": str(a),
                             "difference": str(a - b)})  # fmt: skip
    return rows


def preview_id(employee_code: str, period: PayrollPeriod, change: PayrollChange,
               before: dict[str, Any]) -> str:  # fmt: skip
    """Fingerprint of a preview: the change AND today's numbers it was shown against.
    Apply must present the same id, so data changed after the preview is never applied
    blindly (the user previews again)."""
    import hashlib
    import json

    payload = {"employee": employee_code, "period": str(period),
               "change": change.model_dump(mode="json", exclude={"reason"}),
               "before": {k: {a: str(b) for a, b in v.items()} if isinstance(v, dict) else str(v)
                          for k, v in before.items()}}  # fmt: skip
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def simulate(session: Session, actor: Employee, employee_code: str, period: PayrollPeriod,
             change: PayrollChange) -> dict[str, Any]:  # fmt: skip
    """Before / after / difference for ``change`` - nothing is saved."""
    employee = session.scalar(select(Employee).where(
        Employee.employee_code == employee_code.upper()))  # fmt: skip
    if employee is None:
        raise NotFoundError(f"Employee {employee_code} not found")
    service = PayrollCalculationService(session)
    try:
        before_view = service.calculate(employee.employee_code, period)
    except NotFoundError:
        if not (change.attendance or change.fill_month):
            raise
        before_view = None  # no attendance yet: this change creates it
    savepoint = session.begin_nested()
    try:
        apply_change(session, actor, employee, period, change)
        after_view = PayrollCalculationService(session).calculate(employee.employee_code, period)
    finally:
        savepoint.rollback()
    session.expire_all()
    after = _numbers(after_view)
    before = _numbers(before_view) if before_view is not None else _zero_numbers(after)
    preview = preview_id(employee.employee_code, period, change, before)
    old_warnings = before_view.warnings if before_view is not None else []
    new_warnings = [w for w in after_view.warnings if w not in old_warnings]
    return {
        "employee_code": employee.employee_code, "employee_name": employee.full_name,
        "period": str(period), "period_label": period.label,
        "rows": _compare(before, after),
        "net_change": str(after["net"] - before["net"]),
        "overtime": after_view.adjustments.get("overtime", []),
        "adjustments": after_view.adjustments.get("adjustments", []),
        "warnings": new_warnings,
        "saved": False,
        "preview_id": preview,
        "no_attendance_before": before_view is None,
        "requires_approval": _requires_approval(session, period),
    }  # fmt: skip


def _zero_numbers(after: dict[str, Any]) -> dict[str, Any]:
    return {k: ({h: ZERO for h in v} if isinstance(v, dict) else ZERO) for k, v in after.items()}


def _requires_approval(session: Session, period: PayrollPeriod) -> bool:
    # a workflow setting: today's value applies to changes of any month
    from datetime import date as _date

    from payroll_copilot.services.company_config import get_config

    return bool(get_config(session, _date.today()).get("change_approval", True))

