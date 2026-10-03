"""AI Payroll Audit: check processed payslips against the payroll engine and find anomalies.

For every processed payslip of the month:
  expected = the payroll engine recalculated with today's data and rules
  actual   = the stored payslip
Every mismatch is diagnosed only with causes the data proves (a timestamp shows the LOP
changed after processing, a rule version applied to the whole month reproduces the stored
PF exactly, ...). Anything unproven is reported as "not explained" - never guessed.
Findings are grouped with calculations.qa (one primary cause per employee and category).

Anomalies are checks that need no recalculation: negative net pay or arrears, salary
variance against the previous month, LOP not decided, PF missing, payslip arithmetic,
employees without a payslip.

Runs in the background (AuditRun row with progress) so large companies don't block a
request. Read-only: it never changes payroll.
"""

from collections import Counter
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from payroll_copilot.calculations.pf_rates import rate_sets
from payroll_copilot.calculations.qa import build_employee_qa
from payroll_copilot.calculations.root_cause import explain_net, is_rounding
from payroll_copilot.calculations.validation import (
    ComparisonResult,
    ComponentGroup,
    EmployeeValidation,
    IssueType,
    compare,
)
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PayrollCopilotError
from payroll_copilot.core.money import format_inr
from payroll_copilot.db.base import iso_utc
from payroll_copilot.db.models import (
    AuditRun,
    Employee,
    LopRecord,
    OvertimeEntry,
    PayAdjustment,
    PayrollRun,
    SalaryStructure,
)
from payroll_copilot.domain.enums import PayrollRunStatus
from payroll_copilot.repositories.attendance_repo import AttendanceRepository
from payroll_copilot.services import audit
from payroll_copilot.services.payroll_service import PayrollCalculationService, PayrollView
from payroll_copilot.services.statutory_rates import StatutoryRateService

ZERO = Decimal(0)
TOLERANCE = Decimal(1)  # ₹1: rounding
VARIANCE_LIMIT = Decimal("0.20")  # net pay change vs previous month that is flagged
CATEGORIES = ("Salary structures", "Attendance", "PF", "ESIC", "PT", "TDS", "Net pay")
GROUPS = {
    "EE_PF": ComponentGroup.PF, "ER_EPF": ComponentGroup.PF, "EPS": ComponentGroup.PF,
    "EDLI": ComponentGroup.PF, "EPF_ADMIN": ComponentGroup.PF,
    "EE_ESIC": ComponentGroup.ESIC, "ER_ESIC": ComponentGroup.ESIC, "PT": ComponentGroup.PT,
    "TDS": ComponentGroup.TDS, "LWF_EE": ComponentGroup.LWF, "LWF_ER": ComponentGroup.LWF,
}  # fmt: skip
ISSUE_NAME = {ComponentGroup.PF: "PF mismatch", ComponentGroup.ESIC: "ESIC mismatch",
              ComponentGroup.PT: "PT mismatch", ComponentGroup.TDS: "TDS mismatch",
              ComponentGroup.LWF: "LWF mismatch", ComponentGroup.ATTENDANCE: "LOP / paid days",
              ComponentGroup.EARNINGS: "Earnings mismatch",
              ComponentGroup.TOTALS: "Net pay mismatch",
              ComponentGroup.DATA_QUALITY: "Payslip arithmetic",
              ComponentGroup.OTHER_DEDUCTION: "Deduction mismatch"}  # fmt: skip
CATEGORY_OF_GROUP = {ComponentGroup.ATTENDANCE: "Attendance", ComponentGroup.EARNINGS:
                     "Salary structures", ComponentGroup.ARREAR: "Salary structures",
                     ComponentGroup.PF: "PF", ComponentGroup.ESIC: "ESIC", ComponentGroup.PT: "PT",
                     ComponentGroup.TDS: "TDS", ComponentGroup.LWF: "Net pay",
                     ComponentGroup.OTHER_DEDUCTION: "Net pay", ComponentGroup.TOTALS: "Net pay",
                     ComponentGroup.DATA_QUALITY: "Net pay"}  # fmt: skip


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _lines(slip: dict[str, Any], bucket: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for ln in slip.get(bucket, []):
        out[ln["head_code"]] = out.get(ln["head_code"], ZERO) + Decimal(ln["amount"])
    return out


# ---------------------------------------------------------------------- one employee


class _Facts:
    """What changed after the payslip was processed (timestamps from the database)."""

    def __init__(self, session: Session, employee: Employee, run: PayrollRun,
                 period: PayrollPeriod) -> None:  # fmt: skip
        processed = _aware(run.updated_at or run.created_at)
        lop = session.scalar(select(LopRecord).where(LopRecord.employee_id == employee.id,
                                                     LopRecord.period == str(period)))
        self.lop_changed = bool(lop and _aware(lop.updated_at) > processed)
        self.lop_by = lop and session.get(Employee, lop.updated_by_id)
        revisions = session.scalars(select(SalaryStructure).where(
            SalaryStructure.employee_id == employee.id,
            SalaryStructure.effective_from <= period.end))  # fmt: skip
        changed = [r for r in revisions if _aware(r.created_at) > processed]
        self.salary_changed_from = min((r.effective_from for r in changed), default=None)
        self.overtime_changed = any(
            _aware(o.updated_at) > processed for o in session.scalars(select(OvertimeEntry).where(
                OvertimeEntry.employee_id == employee.id, OvertimeEntry.period == str(period))))
        self.pay_changed = any(
            _aware(a.updated_at) > processed for a in session.scalars(select(PayAdjustment).where(
                PayAdjustment.employee_id == employee.id, PayAdjustment.period == str(period))))
        self.processed = processed


def _pf_cause(session: Session, view: PayrollView, period: PayrollPeriod,
              actual: Decimal) -> tuple[IssueType, str] | None:  # fmt: skip
    """Reproduce the stored PF with each single rule version applied to the whole month."""
    try:
        sets = rate_sets(StatutoryRateService(session).rows(), period.start, period.end)
    except PayrollCopilotError:
        return None
    wage = view.pf.pf_wage_actual
    days = Decimal(period.calendar_days)
    paid = view.salary.salary.paid_days
    earned = wage * paid / days if days else wage
    for rs in sets:
        candidate = (min(earned, rs.pf_ceiling) * rs.ee / 100).quantize(Decimal(1), ROUND_HALF_UP)
        if candidate == actual and len(sets) > 1:
            parts = "; ".join(f"{s.version} ({format_inr(s.pf_ceiling)} ceiling) {s.start:%d}–"
                              f"{s.end:%d %b}" for s in sets)  # fmt: skip
            return (IssueType.RULE_VERSION,
                    f"Payslip PF {format_inr(actual)} = {rs.ee.normalize():f}% of "
                    f"{format_inr(min(earned, rs.pf_ceiling))}: the {rs.version} ceiling was "
                    f"applied to the whole month. The rules for {period.label} are: {parts}")
    uncapped = (earned * sets[-1].ee / 100).quantize(Decimal(1), ROUND_HALF_UP)
    if uncapped == actual:
        return (IssueType.CEILING, f"Payslip PF {format_inr(actual)} is on the full PF wage "
                                   f"{format_inr(earned)} without the wage ceiling")
    return None


def _diagnose(session: Session, r: ComparisonResult, results: list[ComparisonResult],
              facts: _Facts, view: PayrollView,
              period: PayrollPeriod) -> ComparisonResult:  # fmt: skip
    if not r.is_issue:
        return r
    by = {x.component: x for x in results}
    days_moved = any(by[c].is_issue for c in ("PAID_DAYS", "LOP_DAYS") if c in by)
    gross_moved = "GROSS" in by and by["GROSS"].is_issue
    if r.unit == "INR" and is_rounding(r.difference, TOLERANCE):
        return r.with_diagnosis(IssueType.ROUNDING, [f"Rounding difference of "
                                                     f"{format_inr(abs(r.difference))}"])
    if r.group is ComponentGroup.DATA_QUALITY:
        return r.with_diagnosis(IssueType.DATA_QUALITY,
                                ["The stored payslip's lines do not add up to its total"])
    if r.group is ComponentGroup.ATTENDANCE:
        if facts.lop_changed:
            who = f" by {facts.lop_by.full_name}" if facts.lop_by else ""
            return r.with_diagnosis(IssueType.LOP, [
                f"LOP was changed{who} after payroll was processed: payslip "
                f"{r.fmt(r.actual)}, attendance now {r.fmt(r.expected)}"])
        return r.with_diagnosis(IssueType.LOP, ["Attendance differs from the payslip; it "
                                                "changed after payroll was processed"])
    if r.group is ComponentGroup.EARNINGS:
        if days_moved:
            return r.with_diagnosis(IssueType.UPSTREAM, ["Follows the paid-days change"])
        if r.component == "OVERTIME" and facts.overtime_changed:
            return r.with_diagnosis(IssueType.ARREAR, ["Overtime entries changed after payroll "
                                                       "was processed"])
        if facts.pay_changed and r.component in ("BONUS", "OTHER_EARNING", "OTHER_EARNING_NT"):
            return r.with_diagnosis(IssueType.ARREAR, ["One-off pay changed after payroll was "
                                                       "processed"])
        if facts.salary_changed_from:
            return r.with_diagnosis(IssueType.PRORATION, [
                f"Salary structure changed after processing (revision from "
                f"{facts.salary_changed_from:%d %b %Y})"])
        return r.with_diagnosis(IssueType.UNKNOWN, ["Not explained by the data - investigate "
                                                    "with the expected calculation"])
    if r.component == "EE_PF":
        if gross_moved or days_moved:
            return r.with_diagnosis(IssueType.UPSTREAM, ["Follows the earnings change"])
        found = _pf_cause(session, view, period, r.actual or ZERO)
        if found:
            return r.with_diagnosis(*[found[0]], [found[1]])
        return r.with_diagnosis(IssueType.UNKNOWN, ["PF differs from today's rules and data - "
                                                    "not explained by a rule-version or ceiling "
                                                    "check"])
    if r.group in (ComponentGroup.PF, ComponentGroup.ESIC, ComponentGroup.LWF) and (
            "EE_PF" in by and by["EE_PF"].is_issue or gross_moved):
        return r.with_diagnosis(IssueType.UPSTREAM, ["Follows the PF / earnings difference"])
    if r.group in (ComponentGroup.ESIC, ComponentGroup.PT):
        if gross_moved or days_moved:
            return r.with_diagnosis(IssueType.UPSTREAM, ["Follows the earnings change"])
        return r.with_diagnosis(IssueType.SLAB if r.group is ComponentGroup.PT else
                                IssueType.ELIGIBILITY,
                                [f"{r.component} differs although earnings match - the rule or "
                                 "employee settings changed after processing"])
    if r.group is ComponentGroup.TDS:
        if gross_moved or days_moved:
            return r.with_diagnosis(IssueType.UPSTREAM, ["Follows the earnings change"])
        return r.with_diagnosis(IssueType.UNKNOWN, [
            "TDS is re-projected on the year's pay and TDS already deducted; another month of "
            "this financial year changed after processing"])
    if r.group is ComponentGroup.TOTALS:
        text = explain_net(results)
        return r.with_diagnosis(IssueType.UPSTREAM, [text or "Follows the component differences"])
    return r.with_diagnosis(IssueType.UNKNOWN, ["Not explained by the data"])


def check_employee(session: Session, employee: Employee, run: PayrollRun,
                   period: PayrollPeriod) -> tuple[EmployeeValidation, PayrollView]:  # fmt: skip
    view = PayrollCalculationService(session).calculate(employee.employee_code, period)
    slip, code, p = run.payslip, employee.employee_code, str(period)
    s = view.summary
    results = [
        compare(code, p, "PAID_DAYS", ComponentGroup.ATTENDANCE,
                view.salary.salary.paid_days, run.paid_days, unit="DAYS"),
        compare(code, p, "LOP_DAYS", ComponentGroup.ATTENDANCE,
                view.salary.attendance.breakdown["lop_days"], run.lop_days, unit="DAYS"),
    ]  # fmt: skip
    exp_earn = {ln.head_code: ln.amount for ln in s.earnings}
    act_earn = _lines(slip, "earnings")
    for head in dict.fromkeys([*exp_earn, *act_earn]):
        results.append(compare(code, p, head, ComponentGroup.EARNINGS, exp_earn.get(head, ZERO),
                               act_earn.get(head, ZERO), tolerance=TOLERANCE))
    exp_ded = {ln.head_code: ln.amount for ln in s.deductions}
    act_ded = _lines(slip, "deductions")
    for head in dict.fromkeys([*exp_ded, *act_ded]):
        results.append(compare(code, p, head, GROUPS.get(head, ComponentGroup.OTHER_DEDUCTION),
                               exp_ded.get(head, ZERO), act_ded.get(head, ZERO),
                               tolerance=TOLERANCE))  # fmt: skip
    exp_er = {ln.head_code: ln.amount for ln in s.employer_contributions}
    act_er = _lines(slip, "employer_contributions")
    for head in dict.fromkeys([*exp_er, *act_er]):
        results.append(compare(code, p, head, GROUPS.get(head, ComponentGroup.OTHER_DEDUCTION),
                               exp_er.get(head, ZERO), act_er.get(head, ZERO),
                               tolerance=TOLERANCE))  # fmt: skip
    results += [
        compare(code, p, "GROSS", ComponentGroup.TOTALS, s.gross_earnings, run.gross_earnings,
                tolerance=TOLERANCE),
        compare(code, p, "TOTAL_DEDUCTIONS", ComponentGroup.TOTALS, s.total_deductions,
                run.total_deductions, tolerance=TOLERANCE),
        compare(code, p, "NET_PAY", ComponentGroup.TOTALS, s.net_pay, run.net_pay,
                tolerance=TOLERANCE),
        # the stored payslip must add up on its own
        compare(code, p, "HEADER_GROSS", ComponentGroup.DATA_QUALITY, sum(act_earn.values(), ZERO),
                run.gross_earnings),
        compare(code, p, "HEADER_NET", ComponentGroup.DATA_QUALITY,
                run.gross_earnings - sum(act_ded.values(), ZERO), run.net_pay),
    ]  # fmt: skip
    facts = _Facts(session, employee, run, period)
    diagnosed = [_diagnose(session, r, results, facts, view, period) for r in results]
    return EmployeeValidation(employee_code=code, employee_name=employee.full_name, period=p,
                              results=diagnosed), view


# ---------------------------------------------------------------------- anomalies


def _anomaly(employee: Employee, kind: str, severity: str, category: str, message: str,
             **extra: Any) -> dict[str, Any]:  # fmt: skip
    return {"employee_code": employee.employee_code, "employee_name": employee.full_name,
            "issue": kind, "severity": severity, "category": category, "message": message,
            **extra}  # fmt: skip


def anomalies_for(session: Session, employee: Employee, run: PayrollRun,
                  period: PayrollPeriod) -> list[dict[str, Any]]:  # fmt: skip
    out: list[dict[str, Any]] = []
    slip = run.payslip
    if run.net_pay <= 0 and run.paid_days > 0:
        out.append(_anomaly(employee, "Negative net pay", "HIGH", "Net pay",
                            f"Net pay is {format_inr(run.net_pay)} with {run.paid_days} paid days"))
    for ln in slip.get("earnings", []):
        if Decimal(ln["amount"]) < 0:
            out.append(_anomaly(employee, "Negative arrears", "MEDIUM", "Salary structures",
                                f"{ln['name']} is {format_inr(Decimal(ln['amount']))}"
                                + (" (arrear)" if ln.get("is_arrear") else "")))
    prev = session.scalar(select(PayrollRun).where(PayrollRun.employee_id == employee.id,
                                                   PayrollRun.period == str(period.previous())))
    if prev is not None and prev.net_pay > 0:
        change = (run.net_pay - prev.net_pay) / prev.net_pay
        if abs(change) >= VARIANCE_LIMIT:
            hints = []
            if run.lop_days != prev.lop_days:
                hints.append(f"LOP {prev.lop_days} → {run.lop_days} days")
            if run.gross_earnings != prev.gross_earnings:
                hints.append(f"gross {format_inr(prev.gross_earnings)} → "
                             f"{format_inr(run.gross_earnings)}")
            if slip.get("overtime"):
                hints.append("overtime paid")
            if slip.get("adjustments"):
                hints.append("one-off pay")
            out.append(_anomaly(employee, "Salary variance", "MEDIUM", "Net pay",
                                f"Net pay {format_inr(prev.net_pay)} → {format_inr(run.net_pay)} "
                                f"({change * 100:+.0f}% vs {period.previous().label})"
                                + (f": {', '.join(hints)}" if hints else
                                   ": no LOP, gross or one-off change explains it")))
    repo = AttendanceRepository(session)
    absent = sum((e.days for e in repo.exceptions(employee.id, period)), ZERO)
    decided = repo.lop_record(employee.id, period)
    if absent and decided is None:
        out.append(_anomaly(employee, "LOP anomaly", "MEDIUM", "Attendance",
                            f"{absent.normalize():f} absent day(s) without approved leave were "
                            "not decided - no LOP applied"))
    if decided is not None and decided.days > absent and run.lop_days > absent:
        out.append(_anomaly(employee, "LOP anomaly", "LOW", "Attendance",
                            f"LOP {decided.days.normalize():f} days is more than the "
                            f"{absent.normalize():f} absent day(s) recorded"))
    if employee.pf_applicable and run.paid_days > 0 and _lines(slip, "deductions").get(
            "EE_PF", ZERO) == 0:
        out.append(_anomaly(employee, "PF missing", "HIGH", "PF",
                            "PF is applicable but no employee PF was deducted"))
    if not employee.pan and _lines(slip, "deductions").get("TDS", ZERO) > 0:
        out.append(_anomaly(employee, "PAN missing", "LOW", "TDS",
                            "TDS deducted without PAN on record (s.206AA higher rate)"))
    return out


# ---------------------------------------------------------------------- run


def _finding_dict(f: Any, run_locked: bool, results: list[ComparisonResult]) -> dict[str, Any]:
    lead = next((r for r in results if r.component == f.lead_component), None)
    group = lead.group if lead else ComponentGroup.TOTALS
    return {
        "employee_code": f.employee_code, "employee_name": f.employee_name,
        "issue": ISSUE_NAME.get(group, "Mismatch"), "category": CATEGORY_OF_GROUP.get(
            group, "Net pay"),
        "type": f.category.value, "severity": f.severity.value, "title": f.title,
        "component": f.lead_component, "unit": f.unit,
        "expected": None if f.expected is None else str(f.expected),
        "actual": None if f.actual is None else str(f.actual),
        "difference": None if f.difference is None else str(f.difference),
        "components": f.components, "effects": f.effects,
        "causes": lead.root_causes if lead else [], "recommendation": f.recommendation,
        "can_fix": not run_locked,
    }  # fmt: skip


def _health(employees: list[str], issues: list[dict[str, Any]]) -> dict[str, Any]:
    worst: dict[tuple[str, str], str] = {}
    rank = {"HIGH": 2, "MEDIUM": 1, "LOW": 1}
    for i in issues:
        key = (i["employee_code"], i["category"])
        level = "FAIL" if rank.get(i["severity"], 1) == 2 else "WARN"
        if worst.get(key) != "FAIL":
            worst[key] = level
    categories = []
    for cat in CATEGORIES:
        fail = sum(1 for (e, c), v in worst.items() if c == cat and v == "FAIL")
        warn = sum(1 for (e, c), v in worst.items() if c == cat and v == "WARN")
        status = "FAIL" if fail else "WARNING" if warn else "PASS"
        categories.append({"category": cat, "status": status, "failed": fail, "warnings": warn,
                           "passed": len(employees) - fail - warn})
    checks = len(employees) * len(CATEGORIES)
    fails = sum(1 for v in worst.values() if v == "FAIL")
    warns = sum(1 for v in worst.values() if v == "WARN")
    score = round(100 * (checks - fails - warns * Decimal("0.5")) / checks) if checks else 100
    per_employee = {}
    for (e, _), v in worst.items():
        per_employee[e] = "FAIL" if v == "FAIL" or per_employee.get(e) == "FAIL" else "WARN"
    return {"score": int(score), "categories": categories,
            "employees": {"passed": len(employees) - len(per_employee),
                          "warnings": sum(1 for v in per_employee.values() if v == "WARN"),
                          "failed": sum(1 for v in per_employee.values() if v == "FAIL")}}


def start(session: Session, actor: Employee, period: PayrollPeriod,
          department: str | None = None, work_state: str | None = None) -> AuditRun:  # fmt: skip
    from payroll_copilot.services.payroll_source import normalise_state

    state = normalise_state(work_state)
    filters = {k: v for k, v in {"department": department, "work_state": state}.items() if v}
    row = AuditRun(public_id=uuid4().hex[:16], period=str(period), filters=filters,
                   created_by_id=actor.id)  # fmt: skip
    session.add(row)
    session.flush()
    audit.record(session, "ai_audit.start", actor=actor, entity="audit_run",
                 entity_id=row.public_id, new={"period": str(period), "filters": filters})
    return row


def execute(sessions: sessionmaker, public_id: str) -> None:
    """Run an audit (background). Read-only for payroll; only the AuditRun row is written."""
    with sessions() as session:
        row = session.scalar(select(AuditRun).where(AuditRun.public_id == public_id))
        if row is None or row.status != "RUNNING":
            return
        try:
            _execute(session, row)
            row.status = "COMPLETED"
        except Exception as exc:  # noqa: BLE001 - stored on the run, shown in the UI
            session.rollback()
            row = session.scalar(select(AuditRun).where(AuditRun.public_id == public_id))
            row.status, row.error = "FAILED", str(exc)[:500]
        row.finished_at = _now()
        session.commit()


def _execute(session: Session, row: AuditRun) -> None:
    period = PayrollPeriod.parse(row.period)
    stmt = select(Employee).where(Employee.doj <= period.end,
                                  or_(Employee.lwd.is_(None), Employee.lwd >= period.start))
    if row.filters.get("department"):
        stmt = stmt.where(Employee.department == row.filters["department"])
    if row.filters.get("work_state"):
        stmt = stmt.where(Employee.work_state == row.filters["work_state"])
    employees = list(session.scalars(stmt.order_by(Employee.employee_code)))
    runs = {r.employee_id: r for r in session.scalars(select(PayrollRun).where(
        PayrollRun.period == row.period, PayrollRun.employee_id.in_([e.id for e in employees])))}
    row.total = len(employees)
    session.commit()

    findings: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    impact: Counter[str] = Counter()
    over = under = ZERO
    checked: list[str] = []
    for n, e in enumerate(employees, 1):
        run = runs.get(e.id)
        if run is None:
            anomalies.append(_anomaly(e, "Missing payslip", "HIGH", "Net pay",
                                      f"No payslip processed for {period.label}"))
        else:
            checked.append(e.employee_code)
            try:
                validation, _ = check_employee(session, e, run, period)
            except PayrollCopilotError as exc:
                anomalies.append(_anomaly(e, "Cannot recalculate", "HIGH", "Net pay", str(exc)))
            else:
                qa = build_employee_qa(validation)
                locked = run.status is PayrollRunStatus.LOCKED
                findings += [_finding_dict(f, locked, validation.results) for f in qa.findings
                             if f.category is not IssueType.ROUNDING]  # fmt: skip
                for r in validation.results:
                    group = {ComponentGroup.PF: "PF", ComponentGroup.ESIC: "ESIC",
                             ComponentGroup.PT: "PT", ComponentGroup.TDS: "TDS"}.get(r.group)
                    if group and r.difference and r.component.startswith(("EE_", "PT", "TDS")):
                        impact[group] += r.difference
                if qa.net_pay_difference > 0:
                    over += qa.net_pay_difference
                elif qa.net_pay_difference < 0:
                    under -= qa.net_pay_difference
            anomalies += anomalies_for(session, e, run, period)
        if n % 25 == 0 or n == len(employees):
            row.checked = n
            session.commit()

    issues = findings + anomalies
    health = _health([e.employee_code for e in employees], issues)
    by_issue = Counter(i["issue"] for i in issues)
    row.findings = findings
    row.anomalies = anomalies
    row.health = health
    row.summary = {
        "employees": len(employees), "payslips_checked": len(checked),
        "passed": health["employees"]["passed"], "warnings": health["employees"]["warnings"],
        "failed": health["employees"]["failed"], "findings": len(findings),
        "anomalies": len(anomalies), "by_issue": dict(by_issue.most_common()),
        "by_severity": dict(Counter(i["severity"] for i in issues)),
        "statutory_impact": {k: str(v) for k, v in impact.items()},
        "net_overpaid": str(over), "net_underpaid": str(under),
        "note": "Expected = payroll engine with today's data and rules; actual = stored payslip.",
    }  # fmt: skip


# ---------------------------------------------------------------------- read


def run_dict(session: Session, row: AuditRun, full: bool = True) -> dict[str, Any]:
    by = session.get(Employee, row.created_by_id)
    out = {"id": row.public_id, "period": row.period,
           "period_label": PayrollPeriod.parse(row.period).label, "filters": row.filters,
           "status": row.status, "total": row.total, "checked": row.checked,
           "created_by": by.full_name if by else None, "started_at": iso_utc(row.created_at),
           "finished_at": iso_utc(row.finished_at), "summary": row.summary,
           "health": row.health, "error": row.error}  # fmt: skip
    if full:
        out |= {"findings": row.findings or [], "anomalies": row.anomalies or []}
    return out


def latest(session: Session, period: PayrollPeriod | None = None) -> AuditRun | None:
    stmt = select(AuditRun).where(AuditRun.status == "COMPLETED")
    if period is not None:
        stmt = stmt.where(AuditRun.period == str(period))
    return session.scalar(stmt.order_by(AuditRun.id.desc()))


def list_runs(session: Session, limit: int = 20) -> list[dict[str, Any]]:
    rows = session.scalars(select(AuditRun).order_by(AuditRun.id.desc()).limit(limit))
    return [run_dict(session, r, full=False) for r in rows]
