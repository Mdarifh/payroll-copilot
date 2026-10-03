"""Salary Structures screen: templates, who uses them, and a breakup preview.

Templates live in config/payroll/salary_structures.yaml. The preview runs the same
deterministic engine the seed and payroll use (compute_structure_breakup) - nothing
is calculated in React or by the LLM.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.structure import compute_structure_breakup
from payroll_copilot.core.exceptions import NotFoundError, PayrollCopilotError
from payroll_copilot.db.models import Employee, SalaryStructure
from payroll_copilot.services.employee_directory import _current_structure, basis_text
from payroll_copilot.services.salary_config import salary_heads, structure_templates

TAX_LABELS = {"TAXABLE": "Taxable", "EXEMPT_WITH_LIMIT": "Exempt with limit",
              "EXEMPT_ON_BILLS": "Exempt on bills", "NON_TAXABLE": "Non-taxable"}  # fmt: skip
MAX_PREVIEW_GROSS = Decimal("10000000")


def _template(code: str):
    template = structure_templates().get(code)
    if template is None:
        raise NotFoundError(f"Salary structure {code} not found")
    return template


def _usage(session: Session) -> dict[str, tuple[int, int]]:
    """template code -> (employees currently on it, departments)."""
    cur = _current_structure()
    rows = session.execute(
        select(cur.c.template_code, func.count(), func.count(func.distinct(Employee.department)))
        .join(Employee, Employee.id == cur.c.employee_id)
        .group_by(cur.c.template_code)).all()  # fmt: skip
    return {code: (n, d) for code, n, d in rows}


def list_structures(session: Session) -> dict[str, Any]:
    usage = _usage(session)
    items = [{
        "code": t.code, "name": t.name, "version": t.version,
        "components": len(t.components),
        "employees": usage.get(t.code, (0, 0))[0], "departments": usage.get(t.code, (0, 0))[1],
    } for t in sorted(structure_templates().values(), key=lambda t: t.code)]  # fmt: skip
    custom = sum(n for code, (n, _) in usage.items() if code not in structure_templates())
    return {"items": items, "custom_employees": custom}


def structure_detail(session: Session, code: str) -> dict[str, Any]:
    template = _template(code)
    heads = salary_heads()
    employees, departments = _usage(session).get(code, (0, 0))
    components = []
    for spec in sorted(template.components, key=lambda c: c.sequence):
        head = heads[spec.head_code]
        components.append({
            "head_code": spec.head_code, "name": head.name,
            "basis": basis_text(code, spec.head_code),
            "calc_type": spec.calc_type.value, "tab": head.display_group,
            "tax_treatment": TAX_LABELS.get(head.tax_treatment or "", "Taxable"),
            "tax_note": head.tax_note,
            "pf_wage": head.is_pf_wage, "esic_wage": head.is_esic_wage,
        })  # fmt: skip
    return {"code": template.code, "name": template.name, "version": template.version,
            "components": components, "employees": employees, "departments": departments}


def structure_employees(session: Session, code: str, page: int = 1,
                        page_size: int = 20) -> dict[str, Any]:  # fmt: skip
    _template(code)
    cur = _current_structure()
    gross_heads = [h.code for h in salary_heads().values() if h.part_of_gross]
    latest = (select(SalaryStructure.employee_id,
                     func.max(SalaryStructure.effective_from).label("eff"))
              .where(SalaryStructure.effective_from <= date.today())
              .group_by(SalaryStructure.employee_id).subquery())  # fmt: skip
    stmt = (select(Employee, latest.c.eff)
            .join(cur, cur.c.employee_id == Employee.id)
            .outerjoin(latest, latest.c.employee_id == Employee.id)
            .where(cur.c.template_code == code))  # fmt: skip
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = session.execute(stmt.order_by(Employee.full_name)
                           .offset((page - 1) * page_size).limit(page_size)).all()  # fmt: skip
    # monthly gross = sum of the gross heads of the current version; summed as Decimals
    # here because money is text on SQLite (SQL SUM would go through floats)
    gross: dict[int, Decimal] = {}
    for emp_id, eff in ((e.id, eff) for e, eff in rows if eff is not None):
        amounts = session.scalars(select(SalaryStructure.monthly_amount).where(
            SalaryStructure.employee_id == emp_id, SalaryStructure.effective_from == eff,
            SalaryStructure.head_code.in_(gross_heads)))  # fmt: skip
        gross[emp_id] = sum(amounts, Decimal(0))
    return {
        "total": total, "page": page, "pages": max(1, -(-total // page_size)),
        "items": [{"employee_code": e.employee_code, "full_name": e.full_name,
                   "department": e.department,
                   "monthly_gross": str(gross[e.id]) if e.id in gross else None,
                   "effective_from": eff.isoformat() if eff else None}
                  for e, eff in rows],
    }  # fmt: skip


def preview(code: str, monthly_gross: str) -> dict[str, Any]:
    """Breakup of a monthly gross under a template, with the engine's own steps."""
    template = _template(code)
    try:
        gross = Decimal(monthly_gross)
    except InvalidOperation:
        raise PayrollCopilotError("Monthly gross must be a number") from None
    if not 0 < gross <= MAX_PREVIEW_GROSS:
        raise PayrollCopilotError("Monthly gross must be between ₹1 and ₹1,00,00,000")
    heads = salary_heads()
    result = compute_structure_breakup(template, gross, heads)
    return {
        "code": code, "monthly_gross": str(gross), "annual_gross": str(gross * 12),
        "formula": result.formula,
        "lines": [{"head_code": h, "name": heads[h].name, "basis": basis_text(code, h),
                   "amount": str(a)} for h, a in result.breakdown.items()],
        "total": str(result.value),
        "warnings": result.warnings,
    }  # fmt: skip
