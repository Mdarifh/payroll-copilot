"""Read-only payroll intelligence for chat and screens: find employees, explain a
component, compare two months, and show statutory rules.

Every number and every "cause" comes from the payroll engine, stored payslips or the
database. When something cannot be explained from data it is reported as unexplained -
never guessed. Access is enforced here (HR: everyone; manager: own team without amounts;
employee: self only).
"""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from payroll_copilot.auth.permissions import is_staff
from payroll_copilot.calculations.pf_rates import rate_sets
from payroll_copilot.calculations.trace import CalculationResult
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import (
    NotFoundError,
    PayrollCopilotError,
    PermissionDeniedError,
)
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.models import Employee, PayrollRun, SalaryStructure
from payroll_copilot.domain.enums import Role, RuleType
from payroll_copilot.repositories.salary_repo import SalaryRepository
from payroll_copilot.services.payroll_service import PayrollCalculationService, PayrollView
from payroll_copilot.services.salary_config import salary_heads
from payroll_copilot.services.statutory_rates import StatutoryRateService

ZERO = Decimal(0)
COMPONENTS = {
    "PF": "EE_PF", "ESIC": "EE_ESIC", "PT": "PT", "TDS": "TDS", "LWF": "LWF_EE",
    "GROSS": None, "NET": None, "LOP": None,
}


class InsightError(PayrollCopilotError):
    """A question that cannot be answered as asked (shown to the user)."""


# ---------------------------------------------------------------------- employees


def find_employees(session: Session, actor: Employee, query: str,
                   limit: int = 5) -> list[Employee]:  # fmt: skip
    """Employees the actor may see whose code or name matches ``query``."""
    q = query.strip()
    if not q:
        return []
    stmt = select(Employee)
    if actor.role is Role.MANAGER:
        stmt = stmt.where(or_(Employee.id == actor.id, Employee.manager_id == actor.id))
    elif not is_staff(actor):
        stmt = stmt.where(Employee.id == actor.id)
    exact = session.scalar(stmt.where(func.upper(Employee.employee_code) == q.upper()))
    if exact is not None:
        return [exact]
    words = [w for w in q.lower().split() if len(w) >= 2]
    for w in words:
        stmt = stmt.where(func.lower(Employee.full_name).like(f"%{w}%"))
    return list(session.scalars(stmt.order_by(Employee.full_name).limit(limit)))


def resolve_employee(session: Session, actor: Employee, who: str | None,
                     amounts: bool = True) -> Employee:  # fmt: skip
    """One employee from a code or name (default: the actor). ``amounts`` = the caller
    will show money, which a manager may only see for themself."""
    if not who or who.strip().lower() in {"me", "my", "mine", "self", "mera", "meri", "mujhe"}:
        return actor
    matches = find_employees(session, actor, who)
    if not matches:
        if not is_staff(actor) and actor.role is not Role.MANAGER:
            raise PermissionDeniedError("You can only see your own payroll details")
        raise NotFoundError(f"No employee matches '{who}'")
    if len(matches) > 1:
        names = ", ".join(f"{e.full_name} ({e.employee_code})" for e in matches)
        raise InsightError(f"More than one employee matches '{who}': {names}. Which one?")
    employee = matches[0]
    if amounts and not is_staff(actor) and employee.id != actor.id:
        raise PermissionDeniedError("Salary amounts are visible only to HR and the employee")
    return employee


def employee_profile(session: Session, actor: Employee, who: str) -> dict[str, Any]:
    e = resolve_employee(session, actor, who, amounts=False)
    manager = session.get(Employee, e.manager_id) if e.manager_id else None
    return {
        "employee_code": e.employee_code, "full_name": e.full_name,
        "designation": e.designation, "department": e.department, "location": e.location,
        "work_state": e.work_state, "date_of_joining": e.doj.isoformat(),
        "status": e.status.value, "manager": manager.full_name if manager else None,
        "pf_applicable": e.pf_applicable, "esic_applicable": e.esic_applicable,
        "tax_regime": e.tax_regime.value,
    }  # fmt: skip


# ---------------------------------------------------------------------- explain


def _steps(trace: CalculationResult | None) -> list[dict[str, Any]]:
    if trace is None:
        return []
    return [{"label": s.label, "formula": s.formula,
             "result": None if s.result is None else str(s.result)} for s in trace.steps]


def _stored(session: Session, employee: Employee, period: PayrollPeriod) -> PayrollRun | None:
    return session.scalar(select(PayrollRun).where(PayrollRun.employee_id == employee.id,
                                                   PayrollRun.period == str(period)))


def _stored_amount(run: PayrollRun | None, component: str) -> Decimal | None:
    if run is None:
        return None
    if component == "GROSS":
        return run.gross_earnings
    if component == "NET":
        return run.net_pay
    if component == "LOP":
        return run.lop_days
    code = COMPONENTS[component]
    lines = {ln["head_code"]: Decimal(ln["amount"]) for ln in run.payslip.get("deductions", [])}
    return lines.get(code, ZERO)


def explain_component(session: Session, actor: Employee, who: str | None,
                      period: PayrollPeriod, component: str) -> dict[str, Any]:  # fmt: skip
    component = component.upper()
    if component not in COMPONENTS:
        raise InsightError(f"Can explain {', '.join(COMPONENTS)}; not {component}")
    employee = resolve_employee(session, actor, who)
    view = PayrollCalculationService(session).calculate(employee.employee_code, period)
    deductions = {ln.head_code: ln.amount for ln in view.summary.deductions}
    base = {"employee_code": employee.employee_code, "employee_name": employee.full_name,
            "month": str(period), "month_label": period.label, "component": component}
    if component == "PF":
        pf = view.pf
        info = {"applicable": pf.applicable, "reason": pf.reason,
                "amount": str(deductions.get("EE_PF", ZERO)),
                "employer": {k: str(v) for k, v in pf.amounts.items() if k != "EE_PF"},
                "pf_wage": str(pf.pf_wage), "rule_versions": pf.rule_versions,
                "formula": pf.trace.formula if pf.trace else None, "steps": _steps(pf.trace),
                "segments": [{k: str(v) for k, v in seg.items()
                              if k in ("start", "end", "rule_version", "pf_ceiling", "pf_wage",
                                       "employee_pf", "eps", "lop_days")}
                             for seg in pf.segments]}  # fmt: skip
    elif component == "ESIC":
        es = view.esic
        info = {"applicable": es.applicable and es.covered, "reason": es.reason,
                "amount": str(deductions.get("EE_ESIC", ZERO)),
                "esic_wage": str(es.esic_wage), "threshold": str(es.threshold),
                "formula": es.trace.formula if es.trace else None, "steps": _steps(es.trace)}
    elif component == "PT":
        pt = view.pt
        info = {"applicable": pt.applicable and pt.configured and pt.levied, "reason": pt.reason,
                "amount": str(pt.amount), "state": pt.state_code,
                "formula": pt.trace.formula if pt.trace else None, "steps": _steps(pt.trace)}
    elif component == "TDS":
        t = view.tds
        info = {"applicable": t.applicable and t.configured, "reason": t.reason,
                "amount": str(deductions.get("TDS", ZERO)), "regime": employee.tax_regime.value,
                "annual_tax": str(t.annual.total_tax) if t.annual else None,
                "formula": t.trace.formula if t.trace else None, "steps": _steps(t.trace)}
    elif component == "LWF":
        lw = view.lwf
        info = {"amount": str(deductions.get("LWF_EE", ZERO)), "reason": lw.reason,
                "formula": lw.trace.formula if lw.trace else None, "steps": _steps(lw.trace)}
    elif component == "LOP":
        sal = view.salary
        info = {"amount": str(sal.attendance.breakdown["lop_days"]), "unit": "days",
                "paid_days": str(sal.salary.paid_days), "basis_days": str(sal.salary.basis_days),
                "formula": sal.attendance.formula, "steps": _steps(sal.attendance)}
    else:  # GROSS / NET
        s = view.summary
        info = {"amount": str(s.gross_earnings if component == "GROSS" else s.net_pay),
                "earnings": {heads_name(ln.head_code): str(ln.amount) for ln in s.earnings},
                "deductions": {heads_name(ln.head_code): str(ln.amount) for ln in s.deductions},
                "formula": "Net pay = gross earnings − deductions" if component == "NET" else
                           view.salary.salary.trace.formula,
                "steps": [] if component == "NET" else _steps(view.salary.salary.trace)}
    run = _stored(session, employee, period)
    stored = _stored_amount(run, component)
    note = None
    if stored is not None and str(stored) != info["amount"] and Decimal(info["amount"]) != stored:
        unit = (lambda v: f"{v} days") if component == "LOP" else format_inr
        note = (f"The processed payslip shows {unit(stored)}; recalculating with today's data "
                f"and rules gives {unit(Decimal(info['amount']))}. The payslip was made before "
                "the data or rules changed - re-run payroll for the month to update it.")
    return {**base, **info, "payroll_processed": run is not None,
            "payslip_amount": None if stored is None else str(stored), "difference_note": note,
            "warnings": [w for w in view.warnings if component in w or "PF" in w][:5]}  # fmt: skip


def heads_name(code: str) -> str:
    head = salary_heads().get(code)
    return head.name if head else code


# ---------------------------------------------------------------------- compare


def _numbers(view: PayrollView) -> dict[str, Decimal]:
    s = view.summary
    out = {f"earning:{ln.head_code}": ln.amount for ln in s.earnings}
    out |= {f"deduction:{ln.head_code}": ln.amount for ln in s.deductions}
    out |= {"gross": s.gross_earnings, "deductions": s.total_deductions, "net": s.net_pay,
            "paid_days": view.salary.salary.paid_days,
            "lop_days": view.salary.attendance.breakdown["lop_days"]}  # fmt: skip
    return out


def compare_months(session: Session, actor: Employee, who: str | None, first: PayrollPeriod,
                   second: PayrollPeriod) -> dict[str, Any]:  # fmt: skip
    """What changed between two months and why - causes only from data."""
    if first == second:
        raise InsightError("Choose two different months to compare")
    a_period, b_period = sorted([first, second])
    employee = resolve_employee(session, actor, who)
    calc = PayrollCalculationService(session)
    va, vb = calc.calculate(employee.employee_code, a_period), calc.calculate(
        employee.employee_code, b_period)
    na, nb = _numbers(va), _numbers(vb)
    rows = []
    for key in dict.fromkeys([*na, *nb]):
        x, y = na.get(key, ZERO), nb.get(key, ZERO)
        if x != y or key in ("gross", "net"):
            kind, _, code = key.partition(":")
            label = heads_name(code) if code else {"gross": "Gross earnings",
                                                   "deductions": "Total deductions",
                                                   "net": "Net pay", "paid_days": "Paid days",
                                                   "lop_days": "LOP days"}[kind]
            rows.append({"key": key, "label": label, "before": str(x), "after": str(y),
                         "difference": str(y - x)})

    causes: list[dict[str, str]] = []
    # 1. salary structure revisions taking effect after month A up to the end of month B
    revisions = sorted(set(session.scalars(select(SalaryStructure.effective_from).where(
        SalaryStructure.employee_id == employee.id,
        SalaryStructure.effective_from > a_period.end,
        SalaryStructure.effective_from <= b_period.end))))  # fmt: skip
    repo = SalaryRepository(session)
    for eff in revisions:
        before = repo.assignments_between(employee.id, eff.fromordinal(eff.toordinal() - 1),
                                          eff.fromordinal(eff.toordinal() - 1))  # fmt: skip
        after = repo.assignments_between(employee.id, eff, eff)
        old = before[-1].monthly_gross if before else ZERO
        new = after[-1].monthly_gross if after else ZERO
        causes.append({"type": "SALARY_REVISION",
                       "text": f"Salary revised from {eff}: monthly gross {old} → {new}"})
    # 2. attendance
    # a different month length only matters when it changed the pay (proration)
    if na["lop_days"] != nb["lop_days"] or (na["paid_days"] != nb["paid_days"]
                                            and na["gross"] != nb["gross"]):
        causes.append({"type": "ATTENDANCE",
                       "text": f"LOP {na['lop_days']} → {nb['lop_days']} days; paid days "
                               f"{na['paid_days']} → {nb['paid_days']}"})
    # 3. overtime / one-off pay / arrears present in only one month
    for view, period in ((va, a_period), (vb, b_period)):
        for o in view.adjustments.get("overtime", []):
            causes.append({"type": "OVERTIME",
                           "text": f"Overtime in {period.label}: {o['formula']}"})
        for adj in view.adjustments.get("adjustments", []):
            causes.append({"type": "ADJUSTMENT",
                           "text": f"One-off {adj['kind'].lower()} in {period.label}: "
                                   f"{heads_name(adj['head_code'])} {adj['amount']} "
                                   f"({adj['reason']})"})
        if view.salary.arrears and view.salary.arrears.lines:
            total = sum((ln.amount for ln in view.salary.arrears.lines), ZERO)
            causes.append({"type": "ARREARS", "text": f"Arrears paid in {period.label}: {total}"})
    # 4. statutory rule versions
    if va.pf.rule_versions != vb.pf.rule_versions:
        causes.append({"type": "RULE_CHANGE",
                       "text": f"PF rules {', '.join(va.pf.rule_versions) or '-'} → "
                               f"{', '.join(vb.pf.rule_versions) or '-'}"})
    if vb.tds.monthly_tds != va.tds.monthly_tds and not any(c["type"] == "SALARY_REVISION"
                                                             for c in causes):
        causes.append({"type": "TDS",
                       "text": f"Monthly TDS {va.tds.monthly_tds} → {vb.tds.monthly_tds} "
                               "(re-projected on the year's income and TDS already deducted)"})
    net_change = nb["net"] - na["net"]
    return {
        "employee_code": employee.employee_code, "employee_name": employee.full_name,
        "first_month": str(a_period), "second_month": str(b_period),
        "first_label": a_period.label, "second_label": b_period.label,
        "rows": rows, "net_change": str(net_change), "causes": causes,
        "unexplained": bool(net_change) and not causes,
        "note": "Both months recalculated with today's data and rules.",
    }  # fmt: skip


def structure_on(session: Session, employee: Employee, day: date) -> dict[str, Decimal]:
    """Full-month salary components in force on ``day`` (empty if none)."""
    found = SalaryRepository(session).assignments_between(employee.id, day, day)
    return {c.head_code: c.amount for c in found[-1].components} if found else {}


def compare_structures(session: Session, actor: Employee, who: str | None,
                       first: PayrollPeriod, second: PayrollPeriod) -> dict[str, Any]:  # fmt: skip
    """Salary structure (not payroll) of two months - for months payroll cannot be
    calculated for yet (e.g. no attendance)."""
    a_period, b_period = sorted([first, second])
    employee = resolve_employee(session, actor, who)
    old, new = structure_on(session, employee, a_period.end), structure_on(
        session, employee, b_period.end)
    rows = [{"key": f"earning:{h}", "label": heads_name(h), "before": str(old.get(h, ZERO)),
             "after": str(new.get(h, ZERO)),
             "difference": str(new.get(h, ZERO) - old.get(h, ZERO))}
            for h in dict.fromkeys([*old, *new])]  # fmt: skip
    gross = {h for h, info in salary_heads().items() if info.part_of_gross}
    ga = sum((v for h, v in old.items() if h in gross), ZERO)
    gb = sum((v for h, v in new.items() if h in gross), ZERO)
    rows.append({"key": "gross", "label": "Monthly gross (structure)", "before": str(ga),
                 "after": str(gb), "difference": str(gb - ga)})
    revisions = sorted(set(session.scalars(select(SalaryStructure.effective_from).where(
        SalaryStructure.employee_id == employee.id,
        SalaryStructure.effective_from > a_period.end,
        SalaryStructure.effective_from <= b_period.end))))  # fmt: skip
    return {"employee_code": employee.employee_code, "employee_name": employee.full_name,
            "first_month": str(a_period), "second_month": str(b_period),
            "first_label": a_period.label, "second_label": b_period.label, "rows": rows,
            "structure_change": str(gb - ga),
            "causes": [{"type": "SALARY_REVISION", "text": f"Salary revised from {eff}"}
                       for eff in revisions],
            "structure_only": True}  # fmt: skip


# ---------------------------------------------------------------------- statutory rules


def statutory_rules(session: Session, actor: Employee, topic: str, period: PayrollPeriod,
                    who: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The configured rule values the engine will use (not general advice)."""
    from payroll_copilot.rules.context import build_employee_context
    from payroll_copilot.rules.service import RuleService

    topic = topic.upper()
    base = {"topic": topic, "month": str(period), "month_label": period.label,
            "source": "Rules configured in this payroll system (config/rules, statutory_rates)"}
    if topic == "PF":
        sets = rate_sets(StatutoryRateService(session).rows(), period.start, period.end)
        return {**base, "versions": [{
            "version": s.version, "from": s.start.isoformat(), "to": s.end.isoformat(),
            "employee_rate_percent": str(s.ee), "employer_rate_percent": str(s.er),
            "eps_rate_percent": str(s.eps), "edli_rate_percent": str(s.edli),
            "admin_rate_percent": str(s.admin), "pf_wage_ceiling": str(s.pf_ceiling),
            "eps_wage_ceiling": str(s.eps_ceiling), "status": s.status,
        } for s in sets]}  # fmt: skip
    employee = resolve_employee(session, actor, who, amounts=False)
    rules = RuleService(session)
    context = build_employee_context(employee, period)
    kind = {"ESIC": RuleType.ESIC, "PT": RuleType.PT, "TDS": RuleType.TDS,
            "LWF": RuleType.LWF}.get(topic)  # fmt: skip
    if kind is None:
        raise InsightError("Rules are available for PF, ESIC, PT, TDS and LWF")
    rule = rules.resolve(kind, period, context).rule
    ref = {"rule": getattr(rule, "rule_code", topic), "version": getattr(rule, "version", None),
           "effective_from": str(getattr(rule, "effective_from", "")),
           "status": str(getattr(getattr(rule, "status", None), "value", ""))}
    if topic == "ESIC":
        detail = {"employee_rate_percent": str(rule.employee_rate),
                  "employer_rate_percent": str(rule.employer_rate),
                  "wage_threshold": str(rule.wage_threshold)}
    elif topic == "PT":
        detail = {"state": f"{rule.state_name} ({rule.state_code})", "levied": rule.is_levied,
                  "frequency": rule.frequency.value,
                  "slabs": [{"above": str(s.lower_exclusive or 0),
                             "up_to": str(s.upper_inclusive) if s.upper_inclusive else "no limit",
                             "amount": str(s.amount),
                             "gender": s.gender.value if s.gender else "any"}
                            for s in sorted(rule.slabs, key=lambda x: x.sequence)]}
    elif topic == "TDS":
        detail = {"financial_year": rule.financial_year, "regime": rule.regime.value,
                  "standard_deduction": str(rule.standard_deduction)}
    else:
        detail = {"state": f"{rule.state_name} ({rule.state_code})",
                  "employee_amount": str(rule.employee_amount or ""),
                  "employer_amount": str(rule.employer_amount or "")}
    return {**base, **ref, **detail, "applies_to": f"{employee.full_name} "
            f"({employee.employee_code}, {employee.work_state}, {employee.tax_regime.value})"}


def today_period() -> PayrollPeriod:
    return PayrollPeriod.from_date(date.today())
