"""Overtime / adjustments in payroll, LOP edit, salary revision and the what-if preview."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import (
    AuditLog,
    Employee,
    LopRecord,
    OvertimeEntry,
    PayAdjustment,
    PayrollRun,
    SalaryStructure,
)
from payroll_copilot.domain.enums import PayrollRunStatus
from payroll_copilot.services import pay_adjustments as adj
from payroll_copilot.services.payroll_service import PayrollCalculationService
from payroll_copilot.services.payslip import process_payroll
from payroll_copilot.services.simulation import PayrollChange, apply_change, simulate

SEP = PayrollPeriod(2026, 9)


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def counts(s) -> tuple[int, ...]:
    return tuple(s.scalar(select(func.count()).select_from(m))
                 for m in (OvertimeEntry, PayAdjustment, LopRecord, SalaryStructure, AuditLog))


def net(s, code) -> Decimal:
    return PayrollCalculationService(s).calculate(code, SEP).summary.net_pay


def lines(view, bucket) -> dict[str, Decimal]:
    return {ln.head_code: ln.amount for ln in getattr(view.summary, bucket)}


def test_overtime_is_paid_in_the_month(seeded_session):
    s = seeded_session
    hr, e = emp(s, "HR-001"), emp(s, "EMP-101")
    q = adj.quote_overtime(s, e, date(2026, 9, 10), "6")
    assert q["amount"] == (q["hours"] * q["rate_per_hour"]).quantize(Decimal(1))
    assert "Overtime policy" in q["rate_source"]
    before = PayrollCalculationService(s).calculate("EMP-101", SEP)
    adj.add_overtime(s, hr, e, date(2026, 9, 10), "6", "Release weekend")
    after = PayrollCalculationService(s).calculate("EMP-101", SEP)
    assert lines(after, "earnings")["OVERTIME"] == q["amount"]
    assert after.summary.gross_earnings == before.summary.gross_earnings + q["amount"]
    assert lines(after, "deductions")["EE_PF"] == lines(before, "deductions")["EE_PF"]  # not PF wage
    assert after.adjustments["overtime"][0]["formula"] == q["formula"]
    assert s.scalar(select(AuditLog).where(AuditLog.action == "overtime.add")) is not None


def test_overtime_validation(seeded_session):
    s = seeded_session
    e = emp(s, "EMP-101")
    with pytest.raises(adj.AdjustmentError, match="per day"):
        adj.quote_overtime(s, e, date(2026, 9, 10), "9")  # policy: 8 h a day
    with pytest.raises(adj.AdjustmentError, match="future"):
        adj.quote_overtime(s, e, date(2099, 1, 1), "2")
    with pytest.raises(adj.AdjustmentError, match="not employed"):
        adj.quote_overtime(s, e, date(1990, 1, 1), "2")
    with pytest.raises(adj.AdjustmentError, match="number"):
        adj.quote_overtime(s, e, date(2026, 9, 10), "abc")


def test_adjustments_earning_deduction_and_cancel(seeded_session):
    s = seeded_session
    hr, e = emp(s, "HR-001"), emp(s, "EMP-101")
    base = net(s, "EMP-101")
    nt = adj.add_adjustment(s, hr, e, SEP, "EARNING", "OTHER_EARNING_NT", "2500", "Relocation bills")
    assert net(s, "EMP-101") == base + 2500  # non-taxable, not PF / PT wage: full amount
    adj.add_adjustment(s, hr, e, SEP, "DEDUCTION", "OTHER_DED", "1000", "Canteen dues")
    assert net(s, "EMP-101") == base + 1500
    adj.cancel(s, hr, nt, "Entered twice by mistake")
    assert net(s, "EMP-101") == base - 1000
    assert s.get(PayAdjustment, nt.id).status == "CANCELLED"  # kept, not deleted
    with pytest.raises(adj.AdjustmentError, match="cannot be used"):
        adj.add_adjustment(s, hr, e, SEP, "EARNING", "EE_PF", "10", "trying statutory")
    with pytest.raises(adj.AdjustmentError, match="reason"):
        adj.add_adjustment(s, hr, e, SEP, "EARNING", "BONUS", "10", "")
    with pytest.raises(adj.AdjustmentError, match="at most"):
        adj.add_adjustment(s, hr, e, SEP, "EARNING", "BONUS", "0", "zero bonus")


def test_locked_month_cannot_change(seeded_session):
    s = seeded_session
    hr, e = emp(s, "HR-001"), emp(s, "EMP-101")
    run = process_payroll(s, e, SEP, run_by=hr)
    run.status = PayrollRunStatus.LOCKED
    s.flush()
    with pytest.raises(adj.AdjustmentError, match="locked"):
        adj.add_adjustment(s, hr, e, SEP, "EARNING", "BONUS", "100", "Diwali bonus")


def test_simulation_saves_nothing(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    s.flush()
    before_counts = counts(s)
    base = net(s, "EMP-101")
    change = PayrollChange(lop_days=Decimal(2), overtime=[{"work_date": "2026-09-12", "hours": 3}],
                           adjustments=[{"kind": "EARNING", "head_code": "BONUS",
                                         "amount": "5000"}],
                           reason="What-if for review")  # fmt: skip
    result = simulate(s, hr, "EMP-101", SEP, change)
    assert counts(s) == before_counts and net(s, "EMP-101") == base  # rolled back
    rows = {r["key"]: r for r in result["rows"]}
    assert rows["lop_days"]["after"] == "2" and Decimal(rows["paid_days"]["difference"]) == -2
    assert "earnings:OVERTIME" in rows and "earnings:BONUS" in rows
    assert Decimal(result["net_change"]) == Decimal(rows["net"]["after"]) - base
    assert not result["saved"]

    # applying the same change gives exactly the previewed numbers
    apply_change(s, hr, emp(s, "EMP-101"), SEP, change)
    assert net(s, "EMP-101") == Decimal(rows["net"]["after"])


def test_simulation_reports_validation_errors(seeded_session):
    s = seeded_session
    with pytest.raises(adj.AdjustmentError, match="per day"):
        simulate(s, emp(s, "HR-001"), "EMP-101", SEP,
                 PayrollChange(overtime=[{"work_date": "2026-09-12", "hours": 12}]))
    with pytest.raises(adj.AdjustmentError, match="Nothing"):
        simulate(s, emp(s, "HR-001"), "EMP-101", SEP, PayrollChange())


def test_salary_revision_preview(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    current = PayrollCalculationService(s).calculate("EMP-101", SEP).summary.gross_earnings
    result = simulate(s, hr, "EMP-101", SEP, PayrollChange(
        salary={"monthly_gross": current + 5000, "effective_from": "2026-09-01"}))
    rows = {r["key"]: r for r in result["rows"]}
    assert Decimal(rows["gross"]["difference"]) == 5000
    assert s.scalar(select(func.count()).select_from(SalaryStructure).where(
        SalaryStructure.effective_from == date(2026, 9, 1),
        SalaryStructure.employee_id == emp(s, "EMP-101").id)) == 0  # not saved
    assert s.scalar(select(PayrollRun).where(PayrollRun.period == "2026-09")) is None
