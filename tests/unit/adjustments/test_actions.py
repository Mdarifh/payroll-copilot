"""Phase 6: apply previewed changes, cancel entries, recalculate a month."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import AuditLog, Employee, OvertimeEntry, PayrollBatch, PayrollRun
from payroll_copilot.db.session import get_session_factory
from payroll_copilot.services import pay_adjustments as adj
from payroll_copilot.services.batch_worker import BatchWorker
from payroll_copilot.services.payroll_actions import (
    StalePreviewError,
    apply,
    cancel_entry,
    recalculate_month,
)
from payroll_copilot.services.payslip import process_payroll
from payroll_copilot.services.simulation import PayrollChange, simulate

SEP = PayrollPeriod(2026, 9)


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def run_of(s, code) -> PayrollRun:
    return s.scalar(select(PayrollRun).where(PayrollRun.employee_id == emp(s, code).id,
                                             PayrollRun.period == "2026-09"))


def change(**kw) -> PayrollChange:
    return PayrollChange(reason="Approved by manager on mail", **kw)


def test_apply_saves_and_recalculates_the_processed_payslip(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    process_payroll(s, emp(s, "EMP-101"), SEP, run_by=hr)
    old_net, old_version = run_of(s, "EMP-101").net_pay, run_of(s, "EMP-101").version
    c = change(overtime=[{"work_date": "2026-09-12", "hours": 3}])
    preview = simulate(s, hr, "EMP-101", SEP, c)
    result = apply(s, hr, "EMP-101", SEP, c, preview["preview_id"])
    assert result["saved"] and result["recalculated"] == ["2026-09"]
    run = run_of(s, "EMP-101")
    assert run.version == old_version + 1
    assert run.net_pay == old_net + Decimal(preview["net_change"])  # exactly as previewed
    assert run.payslip["overtime"][0]["hours"] == "3"
    assert s.scalar(select(AuditLog).where(AuditLog.action == "action.apply")) is not None


def test_stale_preview_and_missing_reason_are_refused(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    c = change(lop_days=Decimal(1))
    preview = simulate(s, hr, "EMP-101", SEP, c)["preview_id"]
    adj.add_adjustment(s, hr, emp(s, "EMP-101"), SEP, "EARNING", "BONUS", "100", "data moved on")
    with pytest.raises(StalePreviewError):
        apply(s, hr, "EMP-101", SEP, c, preview)  # numbers changed after the preview
    bare = PayrollChange(lop_days=Decimal(1))  # default reason "What-if preview"
    with pytest.raises(adj.AdjustmentError, match="reason"):
        apply(s, hr, "EMP-101", SEP, bare, simulate(s, hr, "EMP-101", SEP, bare)["preview_id"])


def test_changing_an_approved_month_resets_approval(seeded_engine):
    from tests.unit.batch.test_lifecycle import calculated_batch

    batch_id = calculated_batch(seeded_engine, work_state="MH")
    factory = get_session_factory(seeded_engine)
    with factory() as s:
        from payroll_copilot.services import batch_service as svc

        svc.validate(s, batch_id, emp(s, "HR-001"))
        svc.approve(s, batch_id, emp(s, "HR-002"))
        s.commit()
        hr = emp(s, "HR-001")
        c = change(adjustments=[{"kind": "EARNING", "head_code": "BONUS", "amount": "1000"}])
        apply(s, hr, "EMP-101", SEP, c, simulate(s, hr, "EMP-101", SEP, c)["preview_id"])
        assert svc.get_batch(s, batch_id).stage is None  # must be validated again
        assert s.scalar(select(AuditLog).where(AuditLog.action == "batch.stage_reset"))


def test_cancel_recalculates(seeded_session):
    s = seeded_session
    hr, e = emp(s, "HR-001"), emp(s, "EMP-101")
    process_payroll(s, e, SEP, run_by=hr)
    base = run_of(s, "EMP-101").net_pay
    entry = adj.add_overtime(s, hr, e, date(2026, 9, 12), "2", "weekend release")
    process_payroll(s, e, SEP, run_by=hr)
    assert run_of(s, "EMP-101").net_pay > base
    out = cancel_entry(s, hr, "overtime", entry.id, "Wrong employee selected")
    assert out["recalculated"] == ["2026-09"] and run_of(s, "EMP-101").net_pay == base
    assert s.get(OvertimeEntry, entry.id).status == "CANCELLED"


def test_recalculate_month_reruns_processed_payslips(seeded_engine):
    factory = get_session_factory(seeded_engine)
    with factory() as s:
        hr = emp(s, "HR-001")
        for code in ("EMP-101", "EMP-102"):
            process_payroll(s, emp(s, code), SEP, run_by=hr)
        # data changes after processing (stored payslip is now stale)
        adj.add_adjustment(s, hr, emp(s, "EMP-101"), SEP, "EARNING", "OTHER_EARNING_NT", "700",
                           "late reimbursement")  # fmt: skip
        stale = run_of(s, "EMP-101").net_pay
        batch = recalculate_month(s, hr, SEP, "Data fixed after processing")
        s.commit()
        batch_id = batch.batch_id
    BatchWorker(factory, retry_delay=0).run_until_idle()
    with factory() as s:
        b = s.scalar(select(PayrollBatch).where(PayrollBatch.batch_id == batch_id))
        assert b.status.value == "COMPLETED" and b.filters["recalculate"] is True
        assert run_of(s, "EMP-101").net_pay == stale + 700 and run_of(s, "EMP-101").version == 2
        from payroll_copilot.domain.enums import BatchStatus
        from payroll_copilot.services.batch_service import BatchConflictError

        b.status = BatchStatus.QUEUED  # another run for the month is still going
        s.flush()
        with pytest.raises(BatchConflictError, match="still in"):
            recalculate_month(s, emp(s, "HR-001"), SEP, "second run while busy")
