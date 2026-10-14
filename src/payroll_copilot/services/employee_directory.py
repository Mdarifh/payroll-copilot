"""Employee Directory: server-side list + salary structure details for a month.

Who sees what (enforced here, never only in React):
  HR        every employee, all amounts
  manager   own team + self in the list; amounts only for self
  employee  only self
All amounts come from salary_structure rows and the payroll engine - React only formats.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, true
from sqlalchemy.orm import Session

from payroll_copilot.auth.permissions import is_staff
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import (
    NotFoundError,
    PayrollCopilotError,
    PermissionDeniedError,
)
from payroll_copilot.db.base import iso_utc
from payroll_copilot.db.models import Employee, PayrollRun, SalaryStructure
from payroll_copilot.domain.enums import CalcType, Role
from payroll_copilot.repositories.salary_repo import SalaryRepository
from payroll_copilot.services.salary_config import salary_heads, structure_templates

ZERO = Decimal(0)
REQUIRED_EARNINGS = ("BASIC", "HRA", "SPECIAL")
TAX_LABELS = {"TAXABLE": "Taxable", "EXEMPT_WITH_LIMIT": "Exempt with limit",
              "EXEMPT_ON_BILLS": "Exempt on bills", "NON_TAXABLE": "Non-taxable"}  # fmt: skip


# ---------------------------------------------------------------------- access


def _visible(actor: Employee):
    """SQL condition: employees the actor may see in the directory."""
    if is_staff(actor):
        return true()
    if actor.role is Role.MANAGER:
        return or_(Employee.id == actor.id, Employee.manager_id == actor.id)
    return Employee.id == actor.id


def _can_see_amounts(actor: Employee, employee: Employee) -> bool:
    return is_staff(actor) or actor.id == employee.id


def _get_visible(session: Session, actor: Employee, code: str) -> Employee:
    employee = session.scalar(select(Employee).where(Employee.employee_code == code.upper()))
    if employee is None:
        raise NotFoundError(f"Employee {code} not found")
    allowed = session.scalar(select(func.count()).select_from(Employee).where(
        Employee.id == employee.id, _visible(actor)))  # fmt: skip
    if not allowed:
        raise PermissionDeniedError("You can only view your own details")
    return employee


# ---------------------------------------------------------------------- structures


def structure_name(code: str | None) -> str:
    if not code:
        return "Custom structure"
    template = structure_templates().get(code)
    return template.name if template else code


def _current_structure():
    """employee_id -> template_code of the latest salary structure version."""
    latest = (select(SalaryStructure.employee_id,
                     func.max(SalaryStructure.effective_from).label("eff"))
              .where(SalaryStructure.effective_from <= date.today())
              .group_by(SalaryStructure.employee_id).subquery())  # fmt: skip
    return (select(SalaryStructure.employee_id,
                   func.max(SalaryStructure.template_code).label("template_code"))
            .join(latest, (latest.c.employee_id == SalaryStructure.employee_id)
                  & (latest.c.eff == SalaryStructure.effective_from))
            .group_by(SalaryStructure.employee_id).subquery())  # fmt: skip


def basis_text(code: str | None, head_code: str) -> str:
    template = structure_templates().get(code or "")
    spec = next((c for c in template.components if c.head_code == head_code), None) if (
        template) else None  # fmt: skip
    if spec is None:
        return "Fixed amount"
    heads = salary_heads()
    match spec.calc_type:
        case CalcType.PERCENT_OF_GROSS:
            return f"{spec.value.normalize():f}% of gross"
        case CalcType.PERCENT_OF_HEAD:
            base = heads[spec.base_head_code].name if spec.base_head_code in heads else "base"
            return f"{spec.value.normalize():f}% of {base}"
        case CalcType.BALANCING:
            return "Balance of gross"
        case _:
            return "Fixed"


# ---------------------------------------------------------------------- list


def filters(session: Session, actor: Employee) -> dict[str, Any]:
    cur = _current_structure()
    base = select(Employee).where(_visible(actor)).subquery()
    departments = session.scalars(select(func.distinct(base.c.department))
                                  .where(base.c.department.is_not(None))
                                  .order_by(base.c.department)).all()  # fmt: skip
    locations = session.scalars(select(func.distinct(base.c.location))
                                .where(base.c.location.is_not(None))
                                .order_by(base.c.location)).all()  # fmt: skip
    codes = session.scalars(select(func.distinct(cur.c.template_code))
                            .join(base, base.c.id == cur.c.employee_id)
                            .where(cur.c.template_code.is_not(None))).all()  # fmt: skip
    return {"departments": departments, "locations": locations,
            "structures": [{"code": c, "name": structure_name(c)} for c in sorted(codes)]}


def list_employees(
    session: Session, actor: Employee, search: str | None = None,
    department: str | None = None, location: str | None = None, structure: str | None = None,
    page: int = 1, page_size: int = 20,
) -> dict[str, Any]:  # fmt: skip
    cur = _current_structure()
    stmt = (select(Employee, cur.c.template_code)
            .outerjoin(cur, cur.c.employee_id == Employee.id)
            .where(_visible(actor)))  # fmt: skip
    if search and search.strip():
        like = f"%{search.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Employee.full_name).like(like),
                              func.lower(Employee.employee_code).like(like)))
    if department:
        stmt = stmt.where(Employee.department == department)
    if location:
        stmt = stmt.where(Employee.location == location)
    if structure:
        stmt = stmt.where(cur.c.template_code == structure)
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = session.execute(stmt.order_by(Employee.full_name, Employee.employee_code)
                           .offset((page - 1) * page_size).limit(page_size)).all()  # fmt: skip
    return {
        "total": total, "page": page, "page_size": page_size,
        "pages": max(1, -(-total // page_size)),
        "items": [{"employee_code": e.employee_code, "full_name": e.full_name,
                   "department": e.department, "location": e.location,
                   "structure_code": t, "structure_name": structure_name(t)}
                  for e, t in rows],
    }  # fmt: skip


# ---------------------------------------------------------------------- details


def _default_month(session: Session, employee: Employee) -> PayrollPeriod:
    latest = session.scalar(select(func.max(PayrollRun.period)).where(
        PayrollRun.employee_id == employee.id))  # fmt: skip
    return PayrollPeriod.parse(latest) if latest else PayrollPeriod.from_date(date.today())


def _net_pay(session: Session, employee: Employee, month: PayrollPeriod) -> dict[str, Any]:
    run = session.scalar(select(PayrollRun).where(PayrollRun.employee_id == employee.id,
                                                  PayrollRun.period == str(month)))
    if run is not None:
        return {"amount": str(run.net_pay), "source": "Processed payslip"}
    try:
        from payroll_copilot.services.payroll_service import PayrollCalculationService

        view = PayrollCalculationService(session).calculate(employee.employee_code, month)
        return {"amount": str(view.summary.net_pay),
                "source": "Expected - payroll not processed yet"}  # fmt: skip
    except PayrollCopilotError as exc:
        return {"amount": None, "source": f"Not available: {exc}"}


def salary_details(session: Session, actor: Employee, code: str,
                   month: PayrollPeriod | None = None) -> dict[str, Any]:  # fmt: skip
    employee = _get_visible(session, actor, code)
    profile = {
        "employee_code": employee.employee_code, "full_name": employee.full_name,
        "designation": employee.designation, "department": employee.department,
        "location": employee.location, "doj": employee.doj.isoformat(),
    }  # fmt: skip
    month = month or _default_month(session, employee)
    first = PayrollPeriod.from_date(employee.doj)
    latest = PayrollPeriod.from_date(date.today())
    base = {"profile": profile, "month": str(month), "month_label": month.label,
            "months": {"min": str(first), "max": str(latest)}}  # fmt: skip

    assignments = SalaryRepository(session).assignments_between(
        employee.id, month.start, month.end)  # fmt: skip
    structure_code = None
    if assignments:
        rows = session.scalars(select(SalaryStructure.template_code).where(
            SalaryStructure.employee_id == employee.id,
            SalaryStructure.effective_from == assignments[-1].effective_from))  # fmt: skip
        structure_code = next(iter(rows), None)
    base["structure"] = {"code": structure_code, "name": structure_name(structure_code)}
    if not _can_see_amounts(actor, employee):
        return {**base, "restricted": True,
                "message": "Salary amounts are visible only to HR and the employee"}
    if month < first:
        raise NotFoundError(f"{employee.full_name} joined in {first.label}; no salary for "
                            f"{month.label}")  # fmt: skip
    if not assignments:
        raise NotFoundError(f"No salary structure for {employee.employee_code} in {month.label}")

    current = assignments[-1]
    heads = salary_heads()
    amounts = {c.head_code: c.amount for c in current.components}
    earnings = []
    for head_code in [*REQUIRED_EARNINGS, *[c for c in amounts if c not in REQUIRED_EARNINGS]]:
        head = heads.get(head_code)
        if head is None or head.display_group != "EARNINGS":
            continue
        earnings.append({"head_code": head_code, "head": head.name,
                         "basis": basis_text(structure_code, head_code),
                         "monthly_amount": str(amounts.get(head_code, ZERO)),
                         "in_structure": head_code in amounts})  # fmt: skip
    allowances = [
        {"head_code": h.code, "head": h.name, "basis": basis_text(structure_code, h.code)
         if h.code in amounts else "Not in structure",
         "monthly_amount": str(amounts.get(h.code, ZERO)), "in_structure": h.code in amounts,
         "tax_treatment": TAX_LABELS.get(h.tax_treatment or "", "Taxable"),
         "tax_note": h.tax_note}
        for h in sorted(heads.values(), key=lambda h: h.display_order)
        if h.display_group == "ALLOWANCES"
    ]  # fmt: skip
    monthly_gross = current.monthly_gross
    revisions = [{"effective_from": a.effective_from.isoformat(),
                  "monthly_gross": str(a.monthly_gross)} for a in assignments]  # fmt: skip
    # a revision after this month (e.g. approved today, effective next month): the screen
    # of an older month says so, otherwise the new salary seems "not applied"
    next_from = session.scalar(select(func.min(SalaryStructure.effective_from)).where(
        SalaryStructure.employee_id == employee.id, SalaryStructure.effective_from > month.end))
    next_revision = None
    if next_from is not None:
        upcoming = SalaryRepository(session).assignments_between(employee.id, next_from,
                                                                 next_from)  # fmt: skip
        if upcoming:
            next_month = PayrollPeriod.from_date(next_from)
            next_revision = {"effective_from": next_from.isoformat(),
                             "monthly_gross": str(upcoming[-1].monthly_gross),
                             "month": str(next_month), "month_label": next_month.label,
                             "viewable": next_month <= latest}  # fmt: skip
    return {
        **base, "restricted": False,
        "effective_from": current.effective_from.isoformat(),
        "revised_in_month": len(assignments) > 1, "revisions": revisions,
        "next_revision": next_revision,
        "monthly_gross": str(monthly_gross), "annual_gross": str(monthly_gross * 12),
        "net_pay": _net_pay(session, employee, month),
        "earnings": {"rows": earnings,
                     "total": str(sum((Decimal(r["monthly_amount"]) for r in earnings), ZERO))},
        "allowances": {"rows": allowances,
                       "total": str(sum((Decimal(r["monthly_amount"]) for r in allowances),
                                        ZERO))},
    }  # fmt: skip


# ---------------------------------------------------------------------- attendance


def attendance_details(session: Session, actor: Employee, code: str,
                       month: PayrollPeriod | None = None) -> dict[str, Any]:  # fmt: skip
    """Monthly attendance summary, paid-days working, daily calendar and LOP decision.

    Attendance is not salary, so a manager sees their team's (HR all, employee self).
    Counts come from AttendanceService - the same numbers payroll uses."""
    from payroll_copilot.repositories.attendance_repo import AttendanceRepository
    from payroll_copilot.services.attendance_service import AttendanceService

    employee = _get_visible(session, actor, code)
    month = month or _default_month(session, employee)
    base = {"employee_code": employee.employee_code, "full_name": employee.full_name,
            "month": str(month), "month_label": month.label,
            "months": {"min": str(PayrollPeriod.from_date(employee.doj)),
                       "max": str(PayrollPeriod.from_date(date.today()))}}  # fmt: skip
    try:
        view = AttendanceService(session).get_attendance(employee.employee_code, month)
    except NotFoundError as exc:
        return {**base, "available": False, "message": str(exc)}

    repo = AttendanceRepository(session)
    lop_by_day, lop_warnings = repo.lop_dates(employee.id, month)
    r, b = view.record, view.paid_days.breakdown
    days = [{"date": d.day.isoformat(), "status": d.status.value, "remarks": d.remarks,
             "lop_days": str(lop_by_day.get(d.day, ZERO))}
            for d in repo._days(employee.id, month)]  # fmt: skip
    unapproved = sum((e.days for e in repo.exceptions(employee.id, month)), ZERO)
    record = repo.lop_record(employee.id, month)
    decided_by = record and session.get(Employee, record.updated_by_id)
    run = session.scalar(select(PayrollRun).where(PayrollRun.employee_id == employee.id,
                                                  PayrollRun.period == str(month)))
    return {
        **base, "available": True,
        "summary": {
            "calendar_days": r.calendar_days, "working_days": str(r.working_days),
            "week_offs": str(r.week_offs), "holidays": str(r.holidays),
            "present_days": str(r.present_days), "paid_leave_days": str(r.paid_leave_days),
            "absent_days": str(r.absent_days), "lop_days": str(r.lop_days),
            "paid_days": str(b["paid_days"]), "basis_days": str(b["basis_days"]),
            "basis": view.basis.value, "unapproved_absences": str(unapproved),
        },
        "paid_days_steps": [{"label": st.label, "formula": st.formula,
                             "result": None if st.result is None else str(st.result)}
                            for st in view.paid_days.steps],
        "paid_days_formula": view.paid_days.formula,
        "lop": record and {
            "days": str(record.days), "reason": record.reason,
            "updated_by": decided_by.full_name if decided_by else None,
            "updated_at": iso_utc(record.updated_at),
        },
        "warnings": [*view.paid_days.warnings, *lop_warnings],
        "payroll": run and {"status": run.status.value, "paid_days": str(run.paid_days),
                            "lop_days": str(run.lop_days), "version": run.version},
        "days": days,
    }  # fmt: skip


def pay_extras(session: Session, actor: Employee, code: str,
               month: PayrollPeriod | None = None) -> dict[str, Any]:  # fmt: skip
    """Overtime and one-off adjustments of a month (amounts: HR and the employee only)."""
    from payroll_copilot.services.pay_adjustments import overtime_overview

    employee = _get_visible(session, actor, code)
    month = month or _default_month(session, employee)
    if not _can_see_amounts(actor, employee):
        return {"month": str(month), "restricted": True,
                "message": "Overtime amounts are visible only to HR and the employee"}
    return {"month": str(month), "month_label": month.label, "restricted": False,
            **overtime_overview(session, employee, month)}
