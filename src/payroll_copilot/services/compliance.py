"""Compliance screen: which statutory rules apply this month, are they configured and
verified, statutory totals (EPF challan) and the latest audit's statutory health."""

from collections import Counter
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.pf_rates import rate_sets
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PayrollCopilotError
from payroll_copilot.db.models import Employee, PayrollRun
from payroll_copilot.domain.enums import RuleType
from payroll_copilot.rules.context import build_employee_context
from payroll_copilot.rules.service import RuleService
from payroll_copilot.services.statutory_rates import StatutoryRateService


def _rule(rules: RuleService, kind: RuleType, employee: Employee,
          period: PayrollPeriod) -> dict[str, Any]:  # fmt: skip
    try:
        resolved = rules.resolve(kind, period, build_employee_context(employee, period))
    except PayrollCopilotError as exc:
        return {"configured": False, "status": "MISSING", "detail": str(exc)}
    r = resolved.rule
    return {"configured": True, "rule": f"{r.rule_code} v{r.version}",
            "status": getattr(r.status, "value", str(r.status)),
            "effective_from": str(r.effective_from),
            "levied": getattr(r, "is_levied", True)}  # fmt: skip


def overview(session: Session, period: PayrollPeriod) -> dict[str, Any]:
    from payroll_copilot.services import payroll_audit as pa
    from payroll_copilot.services.batch_service import pf_challan

    employees = list(session.scalars(select(Employee).where(
        Employee.doj <= period.end, or_(Employee.lwd.is_(None), Employee.lwd >= period.start))))
    rules = RuleService(session)

    try:
        pf = [{"version": s.version, "from": s.start.isoformat(), "to": s.end.isoformat(),
               "employee_rate": str(s.ee), "employer_rate": str(s.er), "eps_rate": str(s.eps),
               "pf_ceiling": str(s.pf_ceiling), "status": s.status}
              for s in rate_sets(StatutoryRateService(session).rows(), period.start, period.end)]
        pf_error = None
    except PayrollCopilotError as exc:
        pf, pf_error = [], str(exc)

    states: dict[str, dict[str, Any]] = {}
    per_state = Counter(e.work_state for e in employees)
    for e in employees:
        if e.work_state in states:
            continue
        states[e.work_state] = {"state": e.work_state, "employees": per_state[e.work_state],
                                "pt": _rule(rules, RuleType.PT, e, period),
                                "lwf": _rule(rules, RuleType.LWF, e, period)}  # fmt: skip
    esic_people = [e for e in employees if e.esic_applicable]
    esic = _rule(rules, RuleType.ESIC, esic_people[0], period) if esic_people else None
    tds: dict[str, dict[str, Any]] = {}
    for e in employees:
        key = e.tax_regime.value
        if key not in tds:
            tds[key] = {**_rule(rules, RuleType.TDS, e, period), "regime": key,
                        "employees": sum(1 for x in employees if x.tax_regime == e.tax_regime)}

    processed = session.scalar(select(PayrollRun.id).where(PayrollRun.period == str(period)))
    challan = pf_challan(session, period) if processed else None
    audit = pa.latest(session, period)
    statutory_health = None
    if audit is not None and audit.health:
        statutory_health = [c for c in audit.health["categories"]
                            if c["category"] in ("PF", "ESIC", "PT", "TDS")]  # fmt: skip
    unverified = [x["version"] for x in pf if x["status"] != "VERIFIED"] + [
        f"PT {s['state']}" for s in states.values()
        if s["pt"].get("status") not in ("VERIFIED", "MISSING")]  # fmt: skip
    return {
        "period": str(period), "label": period.label, "employees": len(employees),
        "pf": {"versions": pf, "error": pf_error, "split_month": len(pf) > 1},
        "esic": {"rule": esic, "employees": len(esic_people)},
        "states": sorted(states.values(), key=lambda s: -s["employees"]),
        "tds": list(tds.values()), "challan": challan,
        "audit": None if audit is None else {"id": audit.public_id, "score": audit.health["score"],
                                             "finished_at": str(audit.finished_at),
                                             "statutory": statutory_health},
        "unverified": unverified,
    }  # fmt: skip
