"""Create the demo ACTUAL payroll run: expected payslips + documented deliberate errors.

Used only to give the QA engine something realistic to check against; a real
integration would import actual payroll from the payroll system instead.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from payroll_copilot.core.config import PROJECT_ROOT
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PayrollCopilotError
from payroll_copilot.core.money import to_decimal
from payroll_copilot.db.models import (
    EmployerContribution,
    Payroll,
    PayrollDeduction,
    PayrollEarning,
    PayrollRun,
)
from payroll_copilot.domain.enums import PayrollRunStatus, PayrollSource
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.rules.loader import read_yaml
from payroll_copilot.services.expected import ExpectedPayrollService

DEFAULT_ACTUAL_FILE = PROJECT_ROOT / "config" / "seed" / "actual_payroll_2026_09.yaml"
ZERO = Decimal(0)


@dataclass
class ActualSeedReport:
    period: str
    created: bool
    employees: int = 0
    injected: dict[str, str] = field(default_factory=dict)


def _amounts(mapping: dict | None) -> dict[str, Decimal]:
    return {k: to_decimal(v) for k, v in (mapping or {}).items()}


def seed_actual_payroll(
    session: Session, path: Path = DEFAULT_ACTUAL_FILE, replace: bool = False
) -> ActualSeedReport:
    data = read_yaml(path)
    period = PayrollPeriod.parse(data["period"])
    report = ActualSeedReport(period=str(period), created=False)

    existing = session.scalar(
        select(PayrollRun).where(
            PayrollRun.period == str(period), PayrollRun.source == PayrollSource.ACTUAL
        )
    )
    if existing and not replace:
        return report
    if existing:
        for slip in existing.payslips:
            session.delete(slip)
        session.flush()
        session.execute(delete(PayrollRun).where(PayrollRun.id == existing.id))
        session.flush()

    overrides: dict[str, dict] = data.get("employees") or {}
    employees = EmployeeRepository(session)
    known = {e.employee_code for e in employees.list_all()}
    unknown = [code for code in overrides if code not in known]
    if unknown:
        raise PayrollCopilotError(f"Actual payroll overrides for unknown employees: {unknown}")

    expected_service = ExpectedPayrollService(session)
    heads = expected_service.payroll.salary_service.salary.heads()
    run = PayrollRun(
        period=str(period), source=PayrollSource.ACTUAL, status=PayrollRunStatus.PROCESSED,
        processed_at=datetime.now(UTC),
    )  # fmt: skip
    session.add(run)

    for employee in employees.list_all():
        if employee.doj > period.end or (employee.lwd and employee.lwd < period.start):
            continue
        view = expected_service.payslip(employee, period)
        s = view.summary
        change = overrides.get(employee.employee_code, {})
        if change:
            report.injected[employee.employee_code] = change.get("note", "")

        if "earnings" in change:
            earnings = [(h, a, False) for h, a in _amounts(change["earnings"]).items()]
        else:
            earnings = [(b.head_code, b.amount, b.is_arrear) for b in s.earnings]
        deductions = {b.head_code: ZERO for b in s.deductions}
        for b in s.deductions:
            deductions[b.head_code] += b.amount
        deductions.update(_amounts(change.get("deductions")))
        employer = {b.head_code: ZERO for b in s.employer_contributions}
        for b in s.employer_contributions:
            employer[b.head_code] += b.amount
        employer.update(_amounts(change.get("employer")))

        gross = sum((a for h, a, _ in earnings if heads[h].part_of_gross), ZERO)
        total_ded = sum(deductions.values(), ZERO)
        total_er = sum(employer.values(), ZERO)
        slip = Payroll(
            run=run,
            employee_id=employee.id,
            period=str(period),
            paid_days=to_decimal(change.get("paid_days", view.salary.salary.paid_days)),
            lop_days=to_decimal(
                change.get("lop_days", view.salary.attendance.breakdown["lop_days"])
            ),
            gross_earnings=gross,
            total_deductions=total_ded,
            net_pay=to_decimal(change["net_pay"]) if "net_pay" in change else gross - total_ded,
            total_employer_contribution=total_er,
            monthly_ctc=gross + total_er,
        )
        slip.earnings = [
            PayrollEarning(head_code=h, amount=a, is_arrear=arr) for h, a, arr in earnings
        ]
        slip.deductions = [PayrollDeduction(head_code=h, amount=a) for h, a in deductions.items()]
        slip.employer_contributions = [
            EmployerContribution(head_code=h, amount=a) for h, a in employer.items()
        ]
        session.add(slip)
        report.employees += 1

    session.flush()
    report.created = True
    return report
