"""PF for one employee and period, plus comparison across PF rule versions."""

from dataclasses import replace
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.pay_summary import compute_wage_base
from payroll_copilot.calculations.pf import PF_COMPONENTS, PFInput, PFResult, calculate_pf
from payroll_copilot.calculations.pf_rates import rate_set_before, rate_sets
from payroll_copilot.calculations.pf_segments import (
    ENGINE_VERSION,
    PFConfig,
    PFProfile,
    SalaryPeriod,
    calculate_pf_segments,
    normalise_policy,
)
from payroll_copilot.calculations.trace import CalculationResult, RuleRef, TraceStep
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.models import Employee, PFRule
from payroll_copilot.domain.enums import RuleStatus, RuleType
from payroll_copilot.rules.applicability import applies_to
from payroll_copilot.rules.context import build_employee_context
from payroll_copilot.rules.service import RuleService
from payroll_copilot.services.salary_service import SalaryCalculationView, SalaryService
from payroll_copilot.services.statutory_rates import StatutoryRateService

_LABELS = {
    "EE_PF": "Employee PF", "ER_EPF": "Employer EPF", "EPS": "EPS", "EDLI": "EDLI",
    "EPF_ADMIN": "EPF Admin",
}  # fmt: skip


def render_pf(result: PFResult, employee_code: str, period: str) -> str:
    if not result.applicable:
        return f"PF | {employee_code} | {period}: not applicable ({result.reason})"
    lines = [
        f"PF | {employee_code} | {PayrollPeriod.parse(period).label} | rule {result.rule.label} "
        f"[{result.rule.status.value}]",
        f"  PF wage earned {format_inr(result.pf_wage_actual)} | ceiling "
        f"{format_inr(result.ceiling)} | PF wage {format_inr(result.pf_wage)} | EPS wage "
        f"{format_inr(result.eps_wage)} | EDLI wage {format_inr(result.edli_wage)}",
        "  Employee deduction:",
        f"    {'Employee PF':<14} {format_inr(result.amounts['EE_PF']):>10}",
        "  Employer contributions:",
    ]
    for code in PF_COMPONENTS[1:]:
        lines.append(f"    {_LABELS[code]:<14} {format_inr(result.amounts[code]):>10}")
    lines.extend(f"  WARNING: {w}" for w in result.warnings)
    return "\n".join(lines)


class PFComparisonRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_label: str
    is_applicable_rule: bool
    result: PFResult


class PFService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.salary_service = SalaryService(session)
        self.rules = RuleService(session)

    def pf_input(self, employee: Employee, salary: SalaryCalculationView) -> PFInput:
        heads = self.salary_service.salary.heads()
        # Increment arrears get month-wise PF from the arrear engine; exclude them here.
        wage = compute_wage_base("PF_WAGE", salary.salary.earnings_excluding("ARREAR"), heads)
        return PFInput(
            employee_code=employee.employee_code,
            period=salary.period,
            pf_wage_lines=wage.breakdown,
            paid_days=salary.salary.paid_days,
            basis_days=salary.salary.basis_days,
            wage_restricted=employee.pf_wage_restricted,
            eps_eligible=employee.eps_eligible,
            dob=employee.dob,
        )

    # ------------------------------------------------------------------ date-segment engine

    def _config(self, period: PayrollPeriod) -> dict:
        from payroll_copilot.services.company_config import get_config

        return get_config(self.session, period.end)

    def _profile(self, employee: Employee, cfg: dict) -> PFProfile:
        return PFProfile(
            employee_code=employee.employee_code, doj=employee.doj, exit=employee.lwd,
            pf_applicable=employee.pf_applicable, pf_applicable_date=employee.pf_applicable_date,
            dob=employee.dob, eps_eligible=employee.eps_eligible,
            eps_member_from=employee.eps_member_from,
            policy=normalise_policy(employee.pf_wage_policy or cfg["pf_wage_policy"]),
            exempt_reason=employee.pf_exempt_reason,
        )  # fmt: skip

    def salary_timeline(self, employee: Employee, period: PayrollPeriod,
                        cfg: dict) -> list[SalaryPeriod]:  # fmt: skip
        """Full-month PF wage of each salary structure version touching the month."""
        if cfg["pf_wage_basis"] == "STATUTORY_WAGES":
            from payroll_copilot.services.wage_rule import statutory_wages
        heads = self.salary_service.salary.heads()
        timeline = []
        for a in self.salary_service.salary.assignments_between(employee.id, period.start,
                                                                period.end):  # fmt: skip
            if cfg["pf_wage_basis"] == "STATUTORY_WAGES":
                wage = statutory_wages(a.components, heads).wages
            else:
                wage = sum((c.amount for c in a.components
                            if heads.get(c.head_code) and heads[c.head_code].is_pf_wage),
                           Decimal(0))  # fmt: skip
            timeline.append(SalaryPeriod(a.effective_from, a.effective_to, wage))
        return timeline

    def segment_inputs(self, employee: Employee, period: PayrollPeriod):
        cfg = self._config(period)
        rows = StatutoryRateService(self.session).rows()
        sets = rate_sets(rows, period.start, period.end)
        lop, lop_warnings = self.salary_service.attendance.lop_dates(employee.id, period)
        config = PFConfig(ceiling_scope=cfg["pf_ceiling_scope"],
                          divisor=cfg["pf_proration_divisor"],
                          edli_basis=cfg["edli_wage_basis"])  # fmt: skip
        return (cfg, rows, sets, self._profile(employee, cfg),
                self.salary_timeline(employee, period, cfg), lop, lop_warnings, config)

    def segmented(self, employee: Employee, period: PayrollPeriod) -> PFResult:
        cfg, rows, sets, profile, salary, lop, lop_warnings, config = self.segment_inputs(
            employee, period)  # fmt: skip
        res = calculate_pf_segments(profile, period, sets, salary, lop, config)
        if res.status != "OK":
            return PFResult.not_applicable(res.reason or "PF not applicable")
        newest = res.segments[-1].rates
        divisor = Decimal(period.calendar_days if config.divisor == "CALENDAR" else 30)
        ceiling = sum((s.rates.pf_ceiling * s.rates.days / divisor
                       for s in {id(x.rates): x for x in res.segments}.values()),
                      Decimal(0))  # fmt: skip
        comparison = self._comparison(res, rows, period, profile, salary, lop, config)
        ref = RuleRef(rule_type=RuleType.PF, rule_code="EPF",
                      version=int(newest.version.rsplit("v", 1)[-1]) if "v" in newest.version
                      else 1, effective_from=newest.version_from,
                      status=RuleStatus(newest.status))  # fmt: skip
        steps = [TraceStep(label=f"{s.start:%d-%b} to {s.end:%d-%b} · {s.rates.version}",
                           formula=f"earned = {s.monthly_wage} × ({s.days} − LOP {s.lop_days})"
                                   f" / {divisor}; ceiling {s.rates.pf_ceiling} for this rule "
                                   "period",
                           values={"reason": s.reason, "eps_member": s.eps_member},
                           result=s.pf_wage.quantize(Decimal("0.01")))
                 for s in res.segments]  # fmt: skip
        warnings = [*lop_warnings, *res.warnings]
        if newest.admin_min:
            warnings.append(f"EPF admin minimum ₹{newest.admin_min} applies per establishment "
                            "per month (PF challan), not per employee")  # fmt: skip
        trace = CalculationResult(
            component="PF", employee_code=employee.employee_code, period=str(period),
            inputs={"pf_applicable_date": str(profile.pf_applicable_date),
                    "policy": profile.policy, **config.as_dict()},
            rule=ref, formula="PF split by date; each rule period capped at "
                              "ceiling × rule-period days / divisor; rounded once per month",
            steps=steps, raw_value=res.employer_total + res.amounts["EE_PF"],
            value=res.employer_total + res.amounts["EE_PF"], breakdown=res.amounts,
            warnings=warnings,
        )  # fmt: skip
        return PFResult(
            applicable=True, rule=ref, pf_wage_actual=res.earned, ceiling=ceiling,
            pf_wage=res.pf_wage, eps_wage=res.eps_wage, edli_wage=res.edli_wage,
            amounts=res.amounts, employer_total=res.employer_total, trace=trace,
            warnings=warnings, engine=ENGINE_VERSION,
            segments=[s.as_dict() for s in res.segments], rule_versions=res.rule_versions,
            details={"config": {**config.as_dict(), "pf_wage_policy": profile.policy,
                                "pf_wage_basis": cfg["pf_wage_basis"]},
                     "sources": {s.rates.version: list(s.rates.sources) for s in res.segments},
                     "comparison": comparison, "lop_dates": {d.isoformat(): str(v)
                                                             for d, v in sorted(lop.items())},
                     "employer_cost": str(res.employer_cost),
                     "pf_account_credit": str(res.pf_account_credit)},
        )  # fmt: skip

    def _comparison(self, res, rows, period, profile, salary, lop, config) -> dict | None:
        """NEW (as calculated) vs OLD (the previous rule version over the whole month)."""
        newest = res.segments[-1].rates
        baseline = rate_set_before(rows, newest.version_from)
        if baseline is None:
            return None
        old_sets = [replace(baseline, start=period.start, end=period.end)]
        old = calculate_pf_segments(profile, period, old_sets, salary, lop, config)
        if old.status != "OK":
            return None
        a, b = old.amounts, res.amounts

        def row(component: str, group: str, old_v: Decimal, new_v: Decimal) -> dict:
            return {"component": component, "group": group, "old": str(old_v),
                    "new": str(new_v), "difference": str(new_v - old_v)}  # fmt: skip

        ee_delta = b["EE_PF"] - a["EE_PF"]
        cost_delta = res.employer_cost - old.employer_cost
        return {
            "old_rule": baseline.version, "new_rules": res.rule_versions,
            "rows": [
                row("PF wage", "wages", old.pf_wage, res.pf_wage),
                row("Employee PF", "employee", a["EE_PF"], b["EE_PF"]),
                row("Employer EPF", "employer", a["ER_EPF"], b["ER_EPF"]),
                row("EPS", "employer", a["EPS"], b["EPS"]),
                row("EDLI", "employer", a["EDLI"], b["EDLI"]),
                row("EPF admin charges", "employer", a["EPF_ADMIN"], b["EPF_ADMIN"]),
                row("EDLI admin charges", "employer", Decimal(0), Decimal(0)),
                row("Total employer PF cost", "employer", old.employer_cost, res.employer_cost),
                row("Monthly credit to PF account", "impact", old.pf_account_credit,
                    res.pf_account_credit),
            ],
            "net_pay_impact": str(-ee_delta),  # take-home change before TDS
            "employer_cost_impact": str(cost_delta),
        }  # fmt: skip

    def calculate_for_salary(
        self, employee: Employee, period: PayrollPeriod, salary: SalaryCalculationView
    ) -> PFResult:
        if salary.salary.gross_earnings == 0 and not salary.salary.lines:
            return PFResult.not_applicable(f"no earnings in {period}")
        if self._config(period)["pf_segment_engine"]:
            return self.segmented(employee, period)
        if not employee.pf_applicable:
            return PFResult.not_applicable("employee excluded from PF (pf_applicable = false)")
        if salary.salary.gross_earnings == 0 and not salary.salary.lines:
            return PFResult.not_applicable(f"no earnings in {period}")
        if employee.pf_join_date and employee.pf_join_date > period.end:
            return PFResult.not_applicable(f"PF membership starts {employee.pf_join_date}")
        context = build_employee_context(employee, period)
        resolved = self.rules.resolve(RuleType.PF, period, context)
        return calculate_pf(self.pf_input(employee, salary), resolved.rule, resolved.warnings)

    def calculate(self, employee_code: str, period: PayrollPeriod) -> PFResult:
        salary = self.salary_service.calculate(employee_code, period)
        employee = self.salary_service.employees.get_by_code(employee_code)
        return self.calculate_for_salary(employee, period, salary)

    def compare_rule_versions(
        self, employee_code: str, period: PayrollPeriod
    ) -> list[PFComparisonRow]:
        """Same earnings, every PF rule version the employee qualifies for (dates ignored)."""
        salary = self.salary_service.calculate(employee_code, period)
        employee = self.salary_service.employees.get_by_code(employee_code)
        if not employee.pf_applicable:
            return []
        context = build_employee_context(employee, period)
        applicable = self.rules.resolve(RuleType.PF, period, context).rule
        inp = self.pf_input(employee, salary)
        rows = []
        for rule in self.session.scalars(select(PFRule).order_by(PFRule.effective_from)):
            if rule.status is RuleStatus.RETIRED or not applies_to(rule.applicability, context):
                continue
            result = calculate_pf(inp, rule)
            rows.append(
                PFComparisonRow(
                    rule_label=result.rule.label,
                    is_applicable_rule=rule.id == applicable.id,
                    result=result,
                )
            )
        return rows


def render_comparison(rows: list[PFComparisonRow], employee_code: str, period: str) -> str:
    if not rows:
        return f"PF comparison | {employee_code} | {period}: PF not applicable"
    header = f"  {'Component':<14}" + "".join(
        f"{r.rule_label + (' *' if r.is_applicable_rule else ''):>40}" for r in rows
    )
    lines = [f"PF rule comparison | {employee_code} | {period}  (* = rule for this period)", header]
    fields = [("PF wage", "pf_wage"), ("Ceiling", "ceiling"), ("EPS wage", "eps_wage")]
    for label, attr in fields:
        lines.append(
            f"  {label:<14}" + "".join(f"{format_inr(getattr(r.result, attr)):>40}" for r in rows)
        )
    for code in PF_COMPONENTS:
        cells = "".join(f"{format_inr(r.result.amounts[code]):>40}" for r in rows)
        lines.append(f"  {_LABELS[code]:<14}{cells}")
    if len(rows) >= 2:
        old, new = rows[-2].result.amounts, rows[-1].result.amounts
        diffs = ", ".join(f"{_LABELS[c]} {format_inr(new[c] - old[c])}" for c in PF_COMPONENTS)
        lines.append(f"  Change (latest − previous): {diffs}")
    return "\n".join(lines)
