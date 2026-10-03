"""Tax help: old vs new regime for the employee's projected FY salary, and unused
deduction limits (80C, 80D, ...) under the old regime.

Every number comes from the TDS engine (the same rules and FY projection as payroll);
nothing here changes data. Declarations are what the user typed - the system does not
store investment proofs yet.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from payroll_copilot.auth.permissions import Perm, has
from payroll_copilot.calculations.tds import Declaration, TDSResult
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import NotFoundError, PermissionDeniedError
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import RuleType, TaxRegime
from payroll_copilot.rules.context import build_employee_context
from payroll_copilot.rules.service import RuleService
from payroll_copilot.services.pt_service import PTService
from payroll_copilot.services.tds_service import TDSService

ZERO = Decimal(0)
DISCLAIMER = ("Estimate from the payroll engine's projection of this financial year's salary "
              "(tax rules marked UNVERIFIED in the system). It is not tax advice; the regime is "
              "changed through HR during the declaration window.")  # fmt: skip


def _summary(result: TDSResult, regime: str) -> dict[str, Any]:
    if not (result.applicable and result.configured and result.annual):
        return {"regime": regime, "available": False, "reason": result.reason}
    a = result.annual
    return {"regime": regime, "available": True, "rule": result.rule.label if result.rule else "",
            "gross": str(result.gross_taxable_salary),
            "standard_deduction": str(result.standard_deduction),
            "professional_tax": str(result.professional_tax),
            "deductions": {k: str(v) for k, v in result.section_deductions.items()},
            "taxable_income": str(a.taxable_income), "slab_tax": str(a.slab_tax),
            "rebate": str(a.rebate), "surcharge": str(a.surcharge), "cess": str(a.cess),
            "total_tax": str(a.total_tax),
            "monthly_average": str((a.total_tax / 12).quantize(Decimal(1)))}  # fmt: skip


def compare_regimes(session: Session, actor: Employee, employee: Employee, period: PayrollPeriod,
                    declared: dict[str, Decimal] | None = None) -> dict[str, Any]:  # fmt: skip
    if employee.id != actor.id and not has(actor, Perm.VIEW_PAYROLL):
        raise PermissionDeniedError("You can only see your own tax")
    declared = {k.upper(): Decimal(v) for k, v in (declared or {}).items() if Decimal(v) > 0}
    tds = TDSService(session)
    for _ in range(3):  # no attendance yet this month -> the latest month that has it
        try:
            salary = tds.salary_service.calculate(employee.employee_code, period)
            break
        except NotFoundError:
            if period.previous() < PayrollPeriod.from_date(employee.doj):
                raise
            period = period.previous()
    else:
        raise NotFoundError(f"No recent attendance for {employee.employee_code} to project tax")
    pt = PTService(session).calculate_for_salary(employee, period, salary)

    def run(regime: str, decl: dict[str, Decimal]) -> TDSResult:
        return tds.calculate_for_salary(
            employee, period, salary, pt, regime=regime,
            declarations=[Declaration(section_code=k, amount=v) for k, v in decl.items()])

    old = run(TaxRegime.OLD.value, declared)
    new = run(TaxRegime.NEW.value, {})  # the new regime allows no such deductions
    regimes = [_summary(old, "OLD"), _summary(new, "NEW")]
    out: dict[str, Any] = {
        "employee_code": employee.employee_code, "employee_name": employee.full_name,
        "financial_year": period.financial_year.label, "period_label": period.label,
        "current_regime": employee.tax_regime.value, "declared": {k: str(v) for k, v in
                                                                  declared.items()},
        "regimes": regimes, "better": None, "saving": "0", "tips": [], "disclaimer": DISCLAIMER,
    }  # fmt: skip
    if not all(r["available"] for r in regimes):
        return out
    old_tax, new_tax = old.annual.total_tax, new.annual.total_tax
    out["better"] = "SAME" if old_tax == new_tax else ("OLD" if old_tax < new_tax else "NEW")
    out["saving"] = str(abs(old_tax - new_tax))
    # unused old-regime limits: what the old-regime tax would be with each one fully used
    context = build_employee_context(employee, period) | {"tax_regime": TaxRegime.OLD.value}
    limits = RuleService(session).resolve(RuleType.TDS, period, context).rule.deduction_limits
    for section, limit in limits.items():
        unused = Decimal(limit) - min(declared.get(section, ZERO), Decimal(limit))
        if unused <= 0:
            continue
        full = run(TaxRegime.OLD.value, declared | {section: Decimal(limit)}).annual.total_tax
        if full < old_tax:
            out["tips"].append({"section": section, "limit": str(limit), "declared":
                                str(declared.get(section, ZERO)), "unused": str(unused),
                                "old_tax_if_used": str(full), "tax_saved": str(old_tax - full),
                                "beats_new": full < new_tax})  # fmt: skip
    if out["tips"]:
        maxed = declared | {t["section"]: Decimal(t["limit"]) for t in out["tips"]}
        out["old_tax_all_used"] = str(run(TaxRegime.OLD.value, maxed).annual.total_tax)
    return out
