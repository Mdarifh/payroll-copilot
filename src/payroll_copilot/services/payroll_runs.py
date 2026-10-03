"""Payroll Runs screen: months, their batches, and pre-checks before a run.

Pre-checks are cheap, read-only checks over the employees in scope (bulk queries, rule
lookups cached per context) so they stay fast for thousands of employees. They use the
same rule resolution as the payroll engine, so "PT not configured" here means payroll
would really skip PT. The full per-employee validation still runs after calculation
(batch_service.validate).
"""

import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from payroll_copilot.calculations.pf_rates import rate_sets
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PayrollCopilotError, RuleNotFoundError
from payroll_copilot.db.base import iso_utc
from payroll_copilot.db.models import (
    Attendance,
    Employee,
    LopRecord,
    PayrollBatch,
    PayrollRun,
    SalaryStructure,
)
from payroll_copilot.domain.enums import AttendanceStatus, PayrollRunStatus, RuleType
from payroll_copilot.rules.context import build_employee_context
from payroll_copilot.rules.service import RuleService
from payroll_copilot.services import batch_service as batches
from payroll_copilot.services.payroll_source import normalise_state
from payroll_copilot.services.statutory_rates import StatutoryRateService

ZERO = Decimal(0)
MAX_ISSUES = 50  # per check; the count is always complete
CHECKS = (
    ("SALARY", "Salary structures"), ("ATTENDANCE", "Attendance"), ("PF", "PF"),
    ("ESIC", "ESIC"), ("PT", "Professional Tax"), ("TDS", "TDS"),
)  # fmt: skip


# ---------------------------------------------------------------------- months


def _totals(session: Session, periods: list[str]) -> dict[str, dict[str, Any]]:
    """Per month: processed employees, gross / deductions / net (Decimal sums in Python:
    money is text on SQLite), and how many payslips are locked."""
    out: dict[str, dict[str, Any]] = {
        p: {"employees": 0, "gross": ZERO, "deductions": ZERO, "net": ZERO, "locked": 0}
        for p in periods}  # fmt: skip
    rows = session.execute(select(PayrollRun.period, PayrollRun.gross_earnings,
                                  PayrollRun.total_deductions, PayrollRun.net_pay,
                                  PayrollRun.status).where(PayrollRun.period.in_(periods)))
    for p, g, d, n, st in rows:
        t = out[p]
        t["employees"] += 1
        t["gross"] += g
        t["deductions"] += d
        t["net"] += n
        t["locked"] += st is PayrollRunStatus.LOCKED
    return out


def _month_status(totals: dict[str, Any], batch: PayrollBatch | None) -> str:
    if totals["employees"] and totals["locked"] == totals["employees"]:
        return "FINALIZED"
    if batch is not None:
        if batch.stage:
            return batch.stage  # VALIDATED / VALIDATION_FAILED / APPROVED / FINALIZED
        return batch.status.value  # REVIEW / QUEUED / RUNNING / COMPLETED...
    return "PROCESSED" if totals["employees"] else "NOT_STARTED"


def list_months(session: Session, limit: int = 12) -> list[dict[str, Any]]:
    periods = set(session.scalars(select(PayrollRun.period).distinct()))
    periods |= set(session.scalars(select(PayrollBatch.period).distinct()))
    periods.add(str(PayrollPeriod.from_date(date.today())))
    periods = sorted(periods, reverse=True)[:limit]
    totals = _totals(session, periods)
    latest = {b.period: b for b in session.scalars(
        select(PayrollBatch).where(PayrollBatch.period.in_(periods))
        .order_by(PayrollBatch.id))}  # fmt: skip  (last one per month wins)
    batch_counts = dict(session.execute(
        select(PayrollBatch.period, func.count()).where(PayrollBatch.period.in_(periods))
        .group_by(PayrollBatch.period)).all())  # fmt: skip
    return [{
        "period": p, "label": PayrollPeriod.parse(p).label,
        "employees": totals[p]["employees"], "locked": totals[p]["locked"],
        "gross": str(totals[p]["gross"]), "deductions": str(totals[p]["deductions"]),
        "net": str(totals[p]["net"]),
        "status": _month_status(totals[p], latest.get(p)),
        "batches": batch_counts.get(p, 0),
        "latest_batch_id": latest[p].batch_id if p in latest else None,
    } for p in periods]  # fmt: skip


def month_detail(session: Session, period: PayrollPeriod) -> dict[str, Any]:
    p = str(period)
    totals = _totals(session, [p])[p]
    rows = list(session.scalars(select(PayrollBatch).where(PayrollBatch.period == p)
                                .order_by(PayrollBatch.id.desc())))  # fmt: skip
    names = dict(session.execute(select(Employee.id, Employee.full_name).where(Employee.id.in_(
        {b.created_by_id for b in rows} | {b.approved_by_id for b in rows if b.approved_by_id}
        | {b.finalized_by_id for b in rows if b.finalized_by_id}))).all())  # fmt: skip
    return {
        "period": p, "label": period.label,
        "employees": totals["employees"], "locked": totals["locked"],
        "gross": str(totals["gross"]), "deductions": str(totals["deductions"]),
        "net": str(totals["net"]),
        "status": _month_status(totals, rows[0] if rows else None),
        "batches": [{
            **batches.status(session, b),
            "created_at": iso_utc(b.created_at),
            "created_by": names.get(b.created_by_id),
            "approved_by": names.get(b.approved_by_id),
            "finalized_by": names.get(b.finalized_by_id),
        } for b in rows],
    }  # fmt: skip


# ---------------------------------------------------------------------- pre-checks


class _Check:
    def __init__(self, key: str, label: str) -> None:
        self.key, self.label = key, label
        self.failed: set[str] = set()
        self.warned: set[str] = set()
        self.issues: list[dict[str, Any]] = []
        self.counts: Counter[str] = Counter()
        self.notes: list[dict[str, str]] = []  # company-level

    def add(self, severity: str, employee: Employee, message: str) -> None:
        (self.failed if severity == "ERROR" else self.warned).add(employee.employee_code)
        self.counts[severity] += 1
        if len(self.issues) < MAX_ISSUES:
            self.issues.append({"severity": severity, "employee_code": employee.employee_code,
                                "employee_name": employee.full_name, "message": message})

    def note(self, severity: str, message: str) -> None:
        self.notes.append({"severity": severity, "message": message})

    def result(self, scope: int) -> dict[str, Any]:
        company_error = any(n["severity"] == "ERROR" for n in self.notes)
        company_warning = any(n["severity"] == "WARNING" for n in self.notes)
        status = ("FAIL" if self.failed or company_error else
                  "WARNING" if self.warned or company_warning else "PASS")  # fmt: skip
        return {
            "key": self.key, "label": self.label, "status": status,
            "passed": scope - len(self.failed | self.warned),
            "warnings": len(self.warned - self.failed), "failed": len(self.failed),
            "notes": self.notes, "issues": self.issues,
            "issue_count": self.counts["ERROR"] + self.counts["WARNING"],
        }  # fmt: skip


def _scope(session: Session, period: PayrollPeriod, department: str | None,
           work_state: str | None) -> list[Employee]:  # fmt: skip
    stmt = select(Employee).where(
        Employee.doj <= period.end, or_(Employee.lwd.is_(None), Employee.lwd >= period.start))
    if department:
        stmt = stmt.where(Employee.department == department)
    if work_state:
        stmt = stmt.where(Employee.work_state == work_state)
    return list(session.scalars(stmt.order_by(Employee.employee_code)))


def prechecks(session: Session, period: PayrollPeriod, department: str | None = None,
              work_state: str | None = None) -> dict[str, Any]:  # fmt: skip
    try:
        state = normalise_state(work_state)
    except ValueError as exc:
        raise PayrollCopilotError(str(exc)) from exc
    employees = _scope(session, period, department, state)
    ids = [e.id for e in employees]
    checks = {key: _Check(key, label) for key, label in CHECKS}

    # salary structure in force during the month
    with_salary = set(session.scalars(select(SalaryStructure.employee_id).distinct().where(
        SalaryStructure.employee_id.in_(ids), SalaryStructure.effective_from <= period.end,
        or_(SalaryStructure.effective_to.is_(None),
            SalaryStructure.effective_to >= period.start))))  # fmt: skip

    # attendance: days recorded, absences, LOP decided
    days = dict(session.execute(select(Attendance.employee_id, func.count()).where(
        Attendance.employee_id.in_(ids), Attendance.day.between(period.start, period.end))
        .group_by(Attendance.employee_id)).all())  # fmt: skip
    absent: dict[int, Decimal] = defaultdict(Decimal)
    for emp_id, st in session.execute(select(Attendance.employee_id, Attendance.status).where(
            Attendance.employee_id.in_(ids), Attendance.day.between(period.start, period.end),
            Attendance.status.in_([AttendanceStatus.ABSENT, AttendanceStatus.HALF_DAY]))):
        absent[emp_id] += Decimal(1) if st is AttendanceStatus.ABSENT else Decimal("0.5")
    decided = set(session.scalars(select(LopRecord.employee_id).where(
        LopRecord.employee_id.in_(ids), LopRecord.period == str(period))))  # fmt: skip
    locked = set(session.scalars(select(PayrollRun.employee_id).where(
        PayrollRun.employee_id.in_(ids), PayrollRun.period == str(period),
        PayrollRun.status == PayrollRunStatus.LOCKED)))  # fmt: skip

    # PF rule versions for the month (company level)
    pf = checks["PF"]
    try:
        sets = rate_sets(StatutoryRateService(session).rows(), period.start, period.end)
        pf.note("PASS", "PF rules: " + ", ".join(
            f"{s.version} {s.start:%d-%b}–{s.end:%d-%b}" for s in sets))  # fmt: skip
        if len(sets) > 1:
            pf.note("WARNING", f"PF rule changes inside {period.label}: PF is split by date")
    except PayrollCopilotError as exc:
        pf.note("ERROR", str(exc))

    rules = RuleService(session)
    cache: dict[tuple, str | None] = {}

    def rule_missing(rule_type: RuleType, employee: Employee) -> str | None:
        context = build_employee_context(employee, period)
        key = (rule_type, tuple(sorted((k, repr(v)) for k, v in context.items())))
        if key not in cache:
            try:
                rules.resolve(rule_type, period, context)
                cache[key] = None
            except RuleNotFoundError as exc:
                cache[key] = str(exc)
            except PayrollCopilotError as exc:  # ambiguous / unverified rule
                cache[key] = str(exc)
        return cache[key]

    for e in employees:
        if e.id in locked:
            checks["SALARY"].add("WARNING", e, f"Payroll {period.label} is finalized (locked)")
        if e.id not in with_salary:
            checks["SALARY"].add("ERROR", e, f"No salary structure in force in {period.label}")
        employed = (min(period.end, e.lwd or period.end) - max(period.start, e.doj)).days + 1
        recorded = days.get(e.id, 0)
        if recorded == 0:
            checks["ATTENDANCE"].add("ERROR", e, f"No attendance recorded for {period.label}")
        elif recorded < employed:
            checks["ATTENDANCE"].add("WARNING", e, f"Attendance has {recorded} of {employed} "
                                                   "employed days")  # fmt: skip
        if absent.get(e.id) and e.id not in decided:
            checks["ATTENDANCE"].add("WARNING", e, f"{absent[e.id].normalize():f} absent day(s) "
                                                   "without approved leave - decide LOP in the "
                                                   "review")  # fmt: skip
        if e.pf_applicable:
            if e.pf_applicable_date is None:
                pf.add("ERROR", e, "PF Applicable Date is missing")
            if not e.uan:
                pf.add("WARNING", e, "UAN missing (needed for the ECR)")
        elif not e.pf_exempt_reason:
            pf.add("WARNING", e, "PF not applicable but no reason recorded")
        if e.esic_applicable:
            if not e.esic_ip_number:
                checks["ESIC"].add("WARNING", e, "ESIC IP number missing")
            if msg := rule_missing(RuleType.ESIC, e):
                checks["ESIC"].add("ERROR", e, msg)
        if e.pt_applicable and (msg := rule_missing(RuleType.PT, e)):
            checks["PT"].add("WARNING", e, f"PT will NOT be calculated ({e.work_state}): {msg}")
        if msg := rule_missing(RuleType.TDS, e):
            checks["TDS"].add("ERROR", e, f"TDS rule missing: {msg}")
        if not e.pan:
            checks["TDS"].add("WARNING", e, "PAN missing - TDS at the higher rate (s.206AA)")

    no_attendance = sum(1 for e in employees if days.get(e.id, 0) == 0)
    if employees and no_attendance == len(employees):
        checks["ATTENDANCE"].note("ERROR", f"No attendance is recorded for anyone in "
                                           f"{period.label}. Create attendance first (button "
                                           "below), then run payroll.")  # fmt: skip
    if period.end >= date.today():
        checks["ATTENDANCE"].note("WARNING", f"{period.label} is not over yet: days after "
                                             f"{date.today():%d %b} have no attendance and would "
                                             "be paid as normal days")  # fmt: skip
    results = [c.result(len(employees)) for c in checks.values()]
    status = Counter(r["status"] for r in results)
    return {
        "period": str(period), "label": period.label,
        "filters": {k: v for k, v in {"department": department, "work_state": state}.items()
                    if v},
        "employees": len(employees),
        "already_processed": session.scalar(select(func.count()).select_from(PayrollRun).where(
            PayrollRun.employee_id.in_(ids), PayrollRun.period == str(period))) or 0,
        "checks": results,
        "summary": {"passed": status["PASS"], "warnings": status["WARNING"],
                    "failed": status["FAIL"]},
        # employee-level failures only fail those employees in the run (retry after fixing);
        # a company-level error (e.g. no PF rule for the month) blocks the whole run
        "blocking": [n["message"] for r in results for n in r["notes"] if n["severity"] == "ERROR"],
        "can_run": bool(employees) and not any(
            n["severity"] == "ERROR" for r in results for n in r["notes"]),
    }  # fmt: skip


# ---------------------------------------------------------------------- employee register


def register(session: Session, period: PayrollPeriod, search: str | None = None,
             page: int = 1, page_size: int = 20) -> dict[str, Any]:  # fmt: skip
    """Employees of the month with their payslip (or why there is none) - the list HR edits
    LOP / one-off pay / attendance from."""
    from payroll_copilot.db.models import BatchItem

    stmt = select(Employee).where(
        Employee.doj <= period.end, or_(Employee.lwd.is_(None), Employee.lwd >= period.start))
    if search and search.strip():
        like = f"%{search.strip().lower()}%"
        stmt = stmt.where(func.lower(Employee.full_name).like(like)
                          | func.lower(Employee.employee_code).like(like))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    employees = list(session.scalars(stmt.order_by(Employee.full_name)
                                     .offset((page - 1) * page_size).limit(page_size)))
    ids = [e.id for e in employees]
    runs = {r.employee_id: r for r in session.scalars(select(PayrollRun).where(
        PayrollRun.period == str(period), PayrollRun.employee_id.in_(ids)))}
    latest = session.scalar(select(PayrollBatch.id).where(PayrollBatch.period == str(period))
                            .order_by(PayrollBatch.id.desc()))  # fmt: skip
    failed = {} if latest is None else dict(session.execute(
        select(BatchItem.employee_id, BatchItem.error).where(
            BatchItem.batch_pk == latest, BatchItem.status == "FAILED",
            BatchItem.employee_id.in_(ids))).all())  # fmt: skip
    attendance = dict(session.execute(select(Attendance.employee_id, func.count()).where(
        Attendance.employee_id.in_(ids), Attendance.day.between(period.start, period.end))
        .group_by(Attendance.employee_id)).all())  # fmt: skip
    lop = dict(session.execute(select(LopRecord.employee_id, LopRecord.days).where(
        LopRecord.employee_id.in_(ids), LopRecord.period == str(period))).all())
    absent: dict[int, Decimal] = defaultdict(Decimal)
    for emp_id, st in session.execute(select(Attendance.employee_id, Attendance.status).where(
            Attendance.employee_id.in_(ids), Attendance.day.between(period.start, period.end),
            Attendance.status.in_([AttendanceStatus.ABSENT, AttendanceStatus.HALF_DAY]))):
        absent[emp_id] += Decimal(1) if st is AttendanceStatus.ABSENT else Decimal("0.5")
    items = []
    for e in employees:
        run = runs.get(e.id)
        status = ("LOCKED" if run and run.status is PayrollRunStatus.LOCKED else
                  "PROCESSED" if run else "FAILED" if e.id in failed else "NOT_PROCESSED")
        items.append({
            "employee_code": e.employee_code, "full_name": e.full_name,
            "department": e.department, "status": status,
            "attendance_days": attendance.get(e.id, 0),
            # absences without approved leave and no LOP decision yet (no LOP will be cut)
            "undecided_absences": str(absent[e.id]) if e.id not in lop else "0",
            "lop_days": str(run.lop_days if run else lop.get(e.id, ZERO)),
            "paid_days": str(run.paid_days) if run else None,
            "gross": str(run.gross_earnings) if run else None,
            "deductions": str(run.total_deductions) if run else None,
            "net": str(run.net_pay) if run else None,
            "one_off": len((run.payslip or {}).get("adjustments", [])) if run else 0,
            "overtime": len((run.payslip or {}).get("overtime", [])) if run else 0,
            # older rows stored the Python class name ('NotFoundError: ...'): not for HR
            "error": re.sub(r"^[A-Za-z]+Error: ", "", failed[e.id]) if failed.get(e.id) else None,
        })  # fmt: skip
    return {"period": str(period), "label": period.label, "total": total, "page": page,
            "pages": max(1, -(-total // page_size)), "items": items}
