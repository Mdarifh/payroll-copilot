"""Pre-finalization checks for a payroll month (spec section 23).

Each check yields ERROR (blocks approval), WARNING (HR must review) or PASS. Nothing is
guessed: missing data is reported with the exact message the PF engine uses.
"""

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.pf_rates import rate_sets
from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PayrollCopilotError
from payroll_copilot.db.models import Employee, PayrollRun, SalaryStructure
from payroll_copilot.repositories.attendance_repo import AttendanceRepository
from payroll_copilot.services.statutory_rates import StatutoryRateService

ERROR, WARNING, PASS = "ERROR", "WARNING", "PASS"


@dataclass(frozen=True)
class Check:
    severity: str
    code: str
    message: str
    employee_id: int | None = None
    employee_code: str | None = None

    def as_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "message": self.message,
                "employee_code": self.employee_code}  # fmt: skip


def _company_checks(session: Session, period: PayrollPeriod, employees: list[Employee]):
    rows = StatutoryRateService(session).rows()
    try:
        sets = rate_sets(rows, period.start, period.end)
        versions = ", ".join(f"{s.version} {s.start:%d-%b}–{s.end:%d-%b}" for s in sets)
        yield Check(PASS, "PF_RULE_CONFIG", f"PF rules for {period}: {versions}")
        unverified = [s.version for s in sets if s.status != "VERIFIED"]
        if unverified:
            severity = WARNING if get_settings().allow_unverified_rules else ERROR
            yield Check(severity, "PF_RULE_UNVERIFIED",
                        f"PF rule {', '.join(unverified)} is UNVERIFIED")  # fmt: skip
        if len(sets) > 1:
            yield Check(WARNING, "PF_TRANSITION",
                        f"PF rule changes inside {period} ({versions}): review the split "
                        "employees in the PF transition list")  # fmt: skip
    except PayrollCopilotError as exc:
        yield Check(ERROR, "PF_RULE_CONFIG", str(exc))
    for field, label in (("uan", "UAN"), ("pan", "PAN"), ("email", "email")):
        counts = Counter(getattr(e, field) for e in employees if getattr(e, field))
        for value, n in counts.items():
            if n > 1:
                codes = [e.employee_code for e in employees if getattr(e, field) == value]
                yield Check(ERROR, "DUPLICATE_EMPLOYEE",
                            f"Same {label} on {n} employees: {', '.join(codes)}")  # fmt: skip


def _employee_checks(session: Session, e: Employee, period: PayrollPeriod):
    def check(severity: str, code: str, message: str) -> Check:
        return Check(severity, code, message, e.id, e.employee_code)

    if e.doj is None:
        yield check(ERROR, "DOJ_MISSING", "DOJ is missing for this employee. Please configure "
                                          "it before processing payroll.")  # fmt: skip
        return
    if e.lwd and e.lwd < e.doj:
        yield check(ERROR, "EXIT_BEFORE_DOJ", f"Exit date {e.lwd} is before DOJ {e.doj}")
    if e.pf_applicable:
        if e.pf_applicable_date is None:
            yield check(ERROR, "PF_DATE_MISSING", "PF Applicable Date is missing for this "
                        "employee. Please configure it before processing payroll.")  # fmt: skip
        elif e.pf_applicable_date < e.doj:
            yield check(ERROR, "PF_DATE_BEFORE_DOJ",
                        f"PF Applicable Date {e.pf_applicable_date} is before DOJ {e.doj}")
    elif not e.pf_exempt_reason:
        yield check(WARNING, "PF_EXEMPT_REASON", "PF is not applicable but no reason is recorded")

    revisions = session.scalars(select(SalaryStructure.effective_from).where(
        SalaryStructure.employee_id == e.id).distinct())  # fmt: skip
    for eff in revisions:
        if eff < e.doj:
            yield check(WARNING, "REVISION_BEFORE_DOJ",
                        f"Salary revision on {eff} is before DOJ {e.doj}; applied from DOJ")

    record = AttendanceRepository(session).lop_record(e.id, period)
    if record is not None and record.days:
        _, warnings = AttendanceRepository(session).lop_dates(e.id, period)
        for w in warnings:
            yield check(WARNING, "LOP_DATES", w)

    # PF calculation itself (also proves salary structure and rules exist for every day)
    from payroll_copilot.services.pf_service import PFService

    pf_service = PFService(session)
    employed = e.doj <= period.end and (e.lwd is None or e.lwd >= period.start)
    if not employed:
        return
    try:
        pf = pf_service.segmented(e, period)
    except PayrollCopilotError as exc:
        yield check(ERROR, "PF_CALCULATION", str(exc))
        return
    if not pf.applicable:
        yield check(PASS, "PF_NOT_APPLICABLE", f"PF not applicable: {pf.reason}")
    else:
        a = pf.amounts
        if a["ER_EPF"] + a["EPS"] != pf.employer_total:
            yield check(ERROR, "PF_EMPLOYER_SPLIT", "Employer EPF + EPS ≠ employer 12%")
        versions = " + ".join(pf.rule_versions)
        yield check(PASS, "PF_CALCULATED",
                    f"PF wage ₹{pf.pf_wage}, EE ₹{a['EE_PF']}, EPS ₹{a['EPS']} ({versions})")
        if len(pf.segments) > 1 and len(pf.rule_versions) > 1:
            yield check(WARNING, "PF_TRANSITION_CASE",
                        "PF split across rule versions: " + "; ".join(
                            f"{s['start'][8:]}–{s['end'][8:]} {s['rule_version']}"
                            for s in pf.segments))  # fmt: skip
    # coverage that a rule change makes mandatory (EPFO FAQ Q9, Q29)
    yield from (check(WARNING, code, msg) for code, msg in _coverage_hints(pf_service, e, period))

    run = session.scalar(select(PayrollRun).where(PayrollRun.employee_id == e.id,
                                                  PayrollRun.period == str(period)))
    if run is not None and run.net_pay < 0:
        yield check(ERROR, "NEGATIVE_NET_PAY", f"Net pay is negative (₹{run.net_pay})")


def _coverage_hints(pf_service, e: Employee, period: PayrollPeriod) -> list[tuple[str, str]]:
    """A rule version starting inside this month may make PF / EPS mandatory (EPFO FAQ Q9,
    Q29): flag employees whose data was not updated for it. HR decides; nothing changes."""
    rows = StatutoryRateService(pf_service.session).rows()
    try:
        sets = rate_sets(rows, period.start, period.end)
    except PayrollCopilotError:
        return []
    hints: list[tuple[str, str]] = []
    cfg = pf_service._config(period)
    for rs in sets:
        if not period.start <= rs.version_from <= period.end:
            continue  # only versions that begin in this month
        timeline = pf_service.salary_timeline(e, period, cfg)
        wage = next((x.pf_wage for x in reversed(timeline) if x.start <= rs.start), None)
        if wage is None or wage > rs.pf_ceiling:
            continue
        if (not e.pf_applicable and e.employee_category != "INTERN") or (
                e.pf_applicable and e.pf_applicable_date and e.pf_applicable_date > rs.start):
            hints.append(("PF_COVERAGE_REQUIRED",
                          f"PF wage ₹{wage} is within the {rs.version} ceiling ₹{rs.pf_ceiling}:"
                          f" PF must apply from {rs.start} - set the PF applicable date"))
        if (e.pf_applicable and not e.eps_eligible and wage <= rs.eps_ceiling
                and (e.eps_member_from is None or e.eps_member_from > rs.start)):
            hints.append(("EPS_MEMBERSHIP_REQUIRED",
                          f"EPS membership required from {rs.start} (PF wage ₹{wage} ≤ "
                          f"₹{rs.eps_ceiling})"))
    return hints


def validate_month(session: Session, period: PayrollPeriod,
                   employees: list[Employee]) -> list[Check]:  # fmt: skip
    checks = list(_company_checks(session, period, employees))
    for e in employees:
        checks.extend(_employee_checks(session, e, period))
    return checks


def summarise(checks: list[Check]) -> dict:
    counts = Counter(c.severity for c in checks)
    return {"errors": counts.get(ERROR, 0), "warnings": counts.get(WARNING, 0),
            "passed": counts.get(PASS, 0)}  # fmt: skip

