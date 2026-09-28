"""Expected vs actual payroll validation with deterministic root-cause analysis."""

from collections import Counter
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.attendance import diagnose_paid_days
from payroll_copilot.calculations.esic import ESICInput, calculate_esic
from payroll_copilot.calculations.pay_summary import compute_wage_base
from payroll_copilot.calculations.pf import calculate_pf
from payroll_copilot.calculations.pt import select_slab
from payroll_copilot.calculations.root_cause import explain_net, is_rounding, matching_candidates
from payroll_copilot.calculations.validation import (
    ComparisonResult,
    ComponentGroup,
    EmployeeValidation,
    IssueType,
    compare,
)
from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.money import RoundingMode, round_money
from payroll_copilot.db.models import Employee, ESICRule, PFRule, PTRule
from payroll_copilot.domain.enums import ComparisonStatus, HeadCategory, RuleStatus, RuleType
from payroll_copilot.domain.payslip import ActualPayslip
from payroll_copilot.domain.salary import PayLine, SalaryHeadInfo
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.repositories.payroll_repo import PayrollRepository
from payroll_copilot.rules.applicability import applies_to
from payroll_copilot.rules.context import build_employee_context
from payroll_copilot.services.attendance_service import paid_days_basis
from payroll_copilot.services.expected import ExpectedPayrollService
from payroll_copilot.services.payroll_service import PayrollView

ZERO = Decimal(0)
G = ComponentGroup

_DEDUCTION_GROUP = {
    "EE_PF": G.PF, "EE_ESIC": G.ESIC, "PT": G.PT, "TDS": G.TDS, "LWF_EE": G.LWF,
}  # fmt: skip
_EMPLOYER_GROUP = {
    "ER_EPF": G.PF, "EPS": G.PF, "EDLI": G.PF, "EPF_ADMIN": G.PF, "ER_ESIC": G.ESIC,
    "LWF_ER": G.LWF,
}  # fmt: skip
_PF_HEADS = {"EE_PF", "ER_EPF", "EPS", "EDLI", "EPF_ADMIN"}


class PeriodValidation(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: str
    employees: list[EmployeeValidation]

    @property
    def issues(self) -> list[ComparisonResult]:
        return [i for e in self.employees for i in e.issues]

    @property
    def by_group(self) -> dict[str, int]:
        return dict(Counter(i.group.value for i in self.issues))

    @property
    def by_issue_type(self) -> dict[str, int]:
        return dict(Counter((i.issue_type or IssueType.UNKNOWN).value for i in self.issues))

    def to_text(self) -> str:
        with_issues = [e for e in self.employees if e.issues]
        lines = [
            f"Payroll validation | {PayrollPeriod.parse(self.period).label}",
            f"  Employees checked: {len(self.employees)} | with issues: {len(with_issues)} | "
            f"issues: {len(self.issues)}",
            f"  By component group: {self.by_group}",
            f"  By issue type: {self.by_issue_type}",
            "",
        ]
        for e in self.employees:
            mark = "OK " if not e.issues else f"{len(e.issues):>2} "
            top = e.issues[0] if e.issues else None
            summary = (
                f"{top.component}: {top.root_causes[0] if top.root_causes else top.status.value}"
                if top else "all components match"
            )  # fmt: skip
            lines.append(f"  [{mark}] {e.employee_code:<9} {summary}")
        return "\n".join(lines)


class ValidationService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.expected = ExpectedPayrollService(session)
        self.payroll_repo = PayrollRepository(session)
        self.employees = EmployeeRepository(session)

    # ------------------------------------------------------------------ public

    def validate_employee(self, employee_code: str, period: PayrollPeriod) -> EmployeeValidation:
        employee = self.employees.get_by_code(employee_code)
        actual = self.payroll_repo.actual_payslip(employee, period)
        view = self.expected.payslip(employee, period)
        if actual is None:
            result = compare(employee.employee_code, str(period), "PAYSLIP", G.TOTALS,
                             view.summary.net_pay, None)  # fmt: skip
            result = result.with_diagnosis(
                IssueType.MISSING, ["No actual payslip in the payroll run"]
            )
            return EmployeeValidation(
                employee_code=employee.employee_code, employee_name=employee.full_name,
                period=str(period), results=[result], note="No actual payslip found",
            )  # fmt: skip
        results = self._compare(employee, period, view, actual)
        results = [self._diagnose(r, employee, period, view, actual, results) for r in results]
        return EmployeeValidation(
            employee_code=employee.employee_code, employee_name=employee.full_name,
            period=str(period), results=results,
        )  # fmt: skip

    def validate_period(self, period: PayrollPeriod) -> PeriodValidation:
        in_run = set(self.payroll_repo.employee_ids_in_run(period))
        validations = []
        for employee in self.employees.list_all():
            employed = employee.doj <= period.end and not (
                employee.lwd and employee.lwd < period.start
            )
            if not employed and employee.id not in in_run:
                continue
            if not employed:
                result = compare(employee.employee_code, str(period), "PAYSLIP", G.TOTALS, None,
                                 Decimal(1)).with_diagnosis(
                    IssueType.ELIGIBILITY,
                    ["Payslip exists for an employee not employed in period"],
                )
                validations.append(EmployeeValidation(
                    employee_code=employee.employee_code, employee_name=employee.full_name,
                    period=str(period), results=[result], note="Unexpected payslip",
                ))  # fmt: skip
                continue
            validations.append(self.validate_employee(employee.employee_code, period))
        return PeriodValidation(period=str(period), employees=validations)

    # ------------------------------------------------------------------ comparison

    def _compare(
        self, employee: Employee, period: PayrollPeriod, view: PayrollView, actual: ActualPayslip
    ) -> list[ComparisonResult]:
        tol = get_settings().comparison_tolerance
        code, p = employee.employee_code, str(period)
        s = view.summary
        salary = view.salary
        heads = self.expected.payroll.salary_service.salary.heads()
        out: list[ComparisonResult] = []

        def add(component, group, expected, actual_value, unit="INR", trace=None):
            out.append(compare(code, p, component, group, expected, actual_value,
                               tolerance=tol, unit=unit, trace=trace))  # fmt: skip

        add("PAID_DAYS", G.ATTENDANCE, salary.salary.paid_days, actual.paid_days, "DAYS",
            salary.attendance)  # fmt: skip
        add("LOP_DAYS", G.ATTENDANCE, salary.attendance.breakdown["lop_days"], actual.lop_days,
            "DAYS", salary.attendance)  # fmt: skip

        expected_earn: dict[str, Decimal] = {}
        for b in s.earnings:
            expected_earn[b.head_code] = expected_earn.get(b.head_code, ZERO) + b.amount
        for head in _ordered(set(expected_earn) | set(actual.earnings), heads):
            add(head, G.EARNINGS, expected_earn.get(head), actual.earnings.get(head),
                trace=salary.salary.trace)  # fmt: skip
        expected_arrears = sum((b.amount for b in s.earnings if b.is_arrear), ZERO)
        arrear_trace = salary.arrears.trace if salary.arrears else salary.salary.trace
        add("ARREARS", G.ARREAR, expected_arrears, actual.arrear_earnings, trace=arrear_trace)
        add("GROSS", G.TOTALS, s.gross_earnings, actual.gross_earnings, trace=salary.salary.trace)

        exp_ded: dict[str, Decimal] = {}
        for b in s.deductions:
            exp_ded[b.head_code] = exp_ded.get(b.head_code, ZERO) + b.amount
        for head in _ordered(set(exp_ded) | set(actual.deductions), heads):
            add(head, _DEDUCTION_GROUP.get(head, G.OTHER_DEDUCTION), exp_ded.get(head),
                actual.deductions.get(head), trace=_trace_for(head, view))  # fmt: skip
        add("TOTAL_DEDUCTIONS", G.TOTALS, s.total_deductions, actual.total_deductions)
        add("NET_PAY", G.TOTALS, s.net_pay, actual.net_pay)

        exp_er: dict[str, Decimal] = {}
        for b in s.employer_contributions:
            exp_er[b.head_code] = exp_er.get(b.head_code, ZERO) + b.amount
        for head in _ordered(set(exp_er) | set(actual.employer), heads):
            add(head, _EMPLOYER_GROUP.get(head, G.TOTALS), exp_er.get(head),
                actual.employer.get(head), trace=_trace_for(head, view))  # fmt: skip
        add("TOTAL_EMPLOYER", G.TOTALS, s.total_employer_contributions,
            actual.total_employer_contribution)  # fmt: skip

        # internal consistency of the actual payslip (header vs its own lines)
        lines_gross = sum((a for h, a in actual.earnings.items() if heads[h].part_of_gross), ZERO)
        lines_ded = sum(actual.deductions.values(), ZERO)
        add("HEADER_GROSS", G.DATA_QUALITY, lines_gross, actual.gross_earnings)
        add("HEADER_DEDUCTIONS", G.DATA_QUALITY, lines_ded, actual.total_deductions)
        add("HEADER_NET", G.DATA_QUALITY, lines_gross - lines_ded, actual.net_pay)
        return out

    # ------------------------------------------------------------------ root cause

    def _diagnose(
        self, r: ComparisonResult, employee: Employee, period: PayrollPeriod, view: PayrollView,
        actual: ActualPayslip, results: list[ComparisonResult],
    ) -> ComparisonResult:  # fmt: skip
        if not r.is_issue:
            return r
        heads = self.expected.payroll.salary_service.salary.heads()
        if r.group is G.DATA_QUALITY:
            return r.with_diagnosis(IssueType.DATA_QUALITY, [
                f"Actual payslip {r.component.removeprefix('HEADER_').lower()} header "
                f"{r.fmt(r.actual)} does not match its own lines {r.fmt(r.expected)}"
            ])  # fmt: skip
        if r.status is ComparisonStatus.MISSING_ACTUAL:
            return r.with_diagnosis(IssueType.MISSING, [
                f"{r.component} expected {r.fmt(r.expected)} but absent in actual payslip"
            ])  # fmt: skip
        if r.status is ComparisonStatus.MISSING_EXPECTED:
            return r.with_diagnosis(IssueType.ELIGIBILITY, [
                f"{r.component} {r.fmt(r.actual)} in actual payslip but not expected "
                "(check eligibility / applicability)"
            ])  # fmt: skip

        if r.component == "PAID_DAYS":
            record = self.expected.payroll.salary_service.attendance.get(employee.id, period)
            diagnosis = diagnose_paid_days(
                record, r.actual, doj=employee.doj, lwd=employee.lwd,
                basis=paid_days_basis(employee),
            )  # fmt: skip
            kind = IssueType.LOP if any("LOP" in c for c in diagnosis.causes) else (
                IssueType.PRORATION if any("DOJ" in c for c in diagnosis.causes)
                else IssueType.UNKNOWN)  # fmt: skip
            return r.with_diagnosis(kind, diagnosis.causes)
        if r.component == "LOP_DAYS":
            return r.with_diagnosis(IssueType.LOP, [
                f"LOP days {r.fmt(r.actual)} in payroll vs {r.fmt(r.expected)} in attendance"
            ])  # fmt: skip
        if r.component in ("GROSS", "ARREARS") or r.group is G.EARNINGS:
            return self._earnings_cause(r, view, actual, heads)
        if r.component in _PF_HEADS:
            return self._pf_cause(r, employee, period, view, actual, heads)
        if r.component in ("EE_ESIC", "ER_ESIC"):
            return self._esic_cause(r, view, actual, heads)
        if r.component == "PT":
            return self._pt_cause(r, employee, period, view, actual, heads)
        if r.component == "NET_PAY":
            explanation = explain_net(results)
            if explanation:
                return r.with_diagnosis(IssueType.UPSTREAM, [explanation])
            header = next((x for x in results if x.component == "HEADER_NET"), None)
            if header and header.is_issue:
                return r.with_diagnosis(IssueType.DATA_QUALITY, [
                    f"Net pay header {r.fmt(r.actual)} is inconsistent with the payslip lines "
                    f"({header.fmt(header.expected)}); see HEADER_NET"
                ])  # fmt: skip
        if r.component in ("TOTAL_DEDUCTIONS", "TOTAL_EMPLOYER"):
            wanted = HeadCategory.DEDUCTION if r.component == "TOTAL_DEDUCTIONS" else (
                HeadCategory.EMPLOYER_CONTRIBUTION)  # fmt: skip
            parts = [x for x in results if x.difference and x.component in heads
                     and heads[x.component].category is wanted]  # fmt: skip
            if parts:
                return r.with_diagnosis(IssueType.UPSTREAM, [
                    "Sum of component differences: "
                    + ", ".join(f"{x.component} {x.difference:+}" for x in parts)
                ])  # fmt: skip
        if is_rounding(r.difference):
            return r.with_diagnosis(IssueType.ROUNDING, ["Difference within ₹1: rounding"])
        return r.with_diagnosis(IssueType.UNKNOWN, [
            "No known cause reproduces the actual value; review the expected trace"
        ])  # fmt: skip

    def _earnings_cause(self, r, view: PayrollView, actual: ActualPayslip, heads):
        red = view.salary.salary.reduction
        diff = r.difference or ZERO
        if r.component in ("GROSS", "ARREARS"):
            lop_days = view.salary.attendance.breakdown["lop_days"]
            # (cause text, amount that reproduces the difference, issue type)
            candidates = [
                (f"LOP of {lop_days} days not deducted (₹{red['reduction_lop']})",
                 red["reduction_lop"], IssueType.LOP),
                (f"Days before DOJ / after LWD not excluded (₹{red['reduction_not_employed']})",
                 red["reduction_not_employed"], IssueType.PRORATION),
                (f"Salary arrears of ₹{red['arrears']} not paid", -red["arrears"],
                 IssueType.ARREAR),
                (f"LOP reversal of ₹{red['lop_reversal']} not paid", -red["lop_reversal"],
                 IssueType.ARREAR),
            ]  # fmt: skip
            if r.component == "ARREARS":
                candidates = [c for c in candidates if c[2] is IssueType.ARREAR]
            kind_of = {text: kind for text, _, kind in candidates}
            matched = matching_candidates(
                diff, {text: value for text, value, _ in candidates if value}, tolerance=Decimal(3)
            )
            if matched:
                return r.with_diagnosis(kind_of[matched[0]], matched)
        else:
            lines = [ln for ln in view.salary.salary.lines if ln.head_code == r.component]
            regular = [ln for ln in lines if ln.kind == "REGULAR"]
            full = regular[0].full_month_amount if regular else None
            expected_regular = sum((ln.amount for ln in regular), ZERO)
            arrear = sum((ln.amount for ln in lines if ln.is_arrear), ZERO)
            if arrear and diff == -arrear:
                kinds = {ln.kind for ln in lines if ln.is_arrear}
                what = "LOP reversal" if kinds == {"LOP_REVERSAL"} else "arrear"
                return r.with_diagnosis(IssueType.ARREAR, [
                    f"{r.component} {what} of ₹{arrear} missing in actual"
                ])  # fmt: skip
            if full is not None and expected_regular != full and r.actual == full + arrear:
                return r.with_diagnosis(IssueType.PRORATION, [
                    f"{r.component} paid at full-month amount {full}; not prorated by paid days"
                ])  # fmt: skip
        if is_rounding(r.difference):
            return r.with_diagnosis(IssueType.ROUNDING, ["Difference within ₹1: rounding"])
        return r.with_diagnosis(IssueType.UNKNOWN, ["Earnings differ; compare line by line"])

    def _actual_wage(self, actual: ActualPayslip, base: str, heads) -> dict[str, Decimal]:
        lines = [PayLine(head_code=h, amount=a) for h, a in actual.earnings.items()]
        return compute_wage_base(base, lines, heads).breakdown

    def _pf_cause(self, r, employee, period, view, actual, heads):
        pf = view.pf
        if not pf.applicable:
            return r.with_diagnosis(IssueType.ELIGIBILITY, [f"PF not expected: {pf.reason}"])
        base_input = self.expected.payroll.pf_service.pf_input(employee, view.salary)
        context = build_employee_context(employee, period)
        causes: list[str] = []
        kind = IssueType.UNKNOWN
        # 1. another PF rule version reproduces the actual value
        for rule in self.session.scalars(select(PFRule).order_by(PFRule.effective_from)):
            if rule.status is RuleStatus.RETIRED or not applies_to(rule.applicability, context):
                continue
            if rule.version == pf.rule.version and rule.rule_code == pf.rule.rule_code:
                continue
            value = calculate_pf(base_input, rule).amounts[r.component]
            if value == r.actual:
                causes.append(
                    f"Actual matches {rule.rule_code} v{rule.version} (PF wage ceiling "
                    f"₹{rule.pf_wage_ceiling}, effective {rule.effective_from}..."
                    f"{rule.effective_to or 'open'}); rule for {period} is {pf.rule.label}"
                )
                kind = IssueType.RULE_VERSION
        # 2. correct rule, but applied to the (wrong) actual PF wage
        if not causes:
            actual_wage = self._actual_wage(actual, "PF_WAGE", heads)
            inp = base_input.model_copy(update={"pf_wage_lines": actual_wage,
                                                "paid_days": actual.paid_days})  # fmt: skip
            rule = self.expected.payroll.pf_service.rules.resolve(
                RuleType.PF, period, context
            ).rule
            if calculate_pf(inp, rule).amounts[r.component] == r.actual:
                wage = sum(actual_wage.values(), ZERO)
                causes.append(
                    f"{r.component} is correct for the actual PF wage ₹{wage}; the difference "
                    "comes from earnings (see GROSS / paid days)"
                )
                kind = IssueType.UPSTREAM
        if not causes and is_rounding(r.difference):
            causes, kind = ["Difference within ₹1: rounding"], IssueType.ROUNDING
        return r.with_diagnosis(kind, causes or ["No PF variant reproduces the actual value"])

    def _esic_cause(self, r, view, actual, heads):
        esic = view.esic
        if not esic.covered:
            return r.with_diagnosis(IssueType.ELIGIBILITY, [f"ESIC not expected: {esic.reason}"])
        rule = _esic_rule(self.session, esic.rule)
        rate = rule.employee_rate if r.component == "EE_ESIC" else rule.employer_rate
        wage = esic.esic_wage
        causes, kind = [], IssueType.UNKNOWN
        raw = wage * rate / 100
        for mode in (RoundingMode.HALF_UP, RoundingMode.FLOOR, RoundingMode.HALF_EVEN):
            rounded = round_money(raw, mode)
            if rounded == r.actual and rounded != r.expected:
                causes.append(
                    f"Actual equals {wage} × {rate}% rounded {mode.value}; rule rounding is "
                    f"{rule.rounding_mode.value} (raw {raw})"
                )
                kind = IssueType.ROUNDING
                break
        if not causes:
            actual_wage = sum(self._actual_wage(actual, "ESIC_WAGE", heads).values(), ZERO)
            recomputed = calculate_esic(
                ESICInput(employee_code=r.employee_code, period=r.period,
                          esic_wage_lines={"actual": actual_wage},
                          coverage_wage=esic.coverage_wage, coverage_date=esic.coverage_date),
                rule,
            ).amounts[r.component]  # fmt: skip
            if recomputed == r.actual:
                causes.append(f"Correct for actual ESIC wage ₹{actual_wage}; see earnings")
                kind = IssueType.UPSTREAM
        return r.with_diagnosis(kind, causes or ["No ESIC variant reproduces the actual value"])

    def _pt_cause(self, r, employee, period, view, actual, heads):
        pt = view.pt
        if not pt.levied or not pt.deduction_month:
            return r.with_diagnosis(IssueType.ELIGIBILITY, [
                f"PT not expected in {period}: {pt.reason}"
            ])  # fmt: skip
        rule = self.session.scalar(select(PTRule).where(
            PTRule.rule_code == pt.rule.rule_code, PTRule.version == pt.rule.version))  # fmt: skip
        causes, kind = [], IssueType.UNKNOWN
        for slab in rule.slabs:
            gender_ok = slab.gender in (None, employee.gender)
            if gender_ok and slab.amount == r.actual and slab.amount != r.expected:
                low = slab.lower_exclusive if slab.lower_exclusive is not None else 0
                high = slab.upper_inclusive if slab.upper_inclusive is not None else "∞"
                causes.append(
                    f"Actual PT ₹{r.actual} is the {low}<wage≤{high} slab; expected slab "
                    f"{pt.slab.band} for PT wage ₹{pt.slab_wage} ({rule.state_name})"
                )
                kind = IssueType.SLAB
                break
        if not causes and pt.wage_basis.value == "MONTHLY":
            wage = sum(self._actual_wage(actual, "PT_WAGE", heads).values(), ZERO)
            if select_slab(rule, employee.gender, wage).amount == r.actual:
                causes.append(f"Correct slab for actual PT wage ₹{wage}; see earnings")
                kind = IssueType.UPSTREAM
        return r.with_diagnosis(kind, causes or ["No PT slab reproduces the actual value"])


def _ordered(codes: set[str], heads: dict[str, SalaryHeadInfo]) -> list[str]:
    return sorted(codes, key=lambda c: (heads[c].display_order if c in heads else 999, c))


def _trace_for(head: str, view: PayrollView):
    if head in view.pf.components:
        return view.pf.components[head]
    if head in view.esic.components:
        return view.esic.components[head]
    return {"PT": view.pt.trace, "TDS": view.tds.trace, "LWF_EE": view.lwf.trace,
            "LWF_ER": view.lwf.trace}.get(head)  # fmt: skip


def _esic_rule(session: Session, ref) -> ESICRule:
    return session.scalar(select(ESICRule).where(
        ESICRule.rule_code == ref.rule_code, ESICRule.version == ref.version))  # fmt: skip
