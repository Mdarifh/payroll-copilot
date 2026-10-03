"""Payroll Runs fixes: finalize locks the whole month, reopen, create attendance for a
month without any, readable failures, and the employee register."""

from datetime import date

import pytest
from sqlalchemy import delete, select

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Attendance, BatchItem, PayrollRun
from payroll_copilot.db.session import get_session_factory
from payroll_copilot.domain.enums import PayrollRunStatus
from payroll_copilot.services import batch_service as svc
from payroll_copilot.services import payroll_runs
from payroll_copilot.services.attendance_edit import create_for_month
from payroll_copilot.services.batch_worker import BatchWorker
from payroll_copilot.services.payslip import process_payroll
from tests.unit.batch.test_lifecycle import SEP, calculated_batch, emp


def test_finalize_locks_payslips_processed_by_an_earlier_batch(seeded_engine):
    factory = get_session_factory(seeded_engine)
    with factory() as s:  # processed outside the batch (e.g. chat / an earlier batch)
        process_payroll(s, emp(s, "EMP-101"), SEP, run_by=emp(s, "HR-001"))
        s.commit()
    batch_id = calculated_batch(seeded_engine)
    with factory() as s:
        items = {i.employee_id: i.status.value for i in s.scalars(select(BatchItem))}
        assert items[emp(s, "EMP-101").id] == "SKIPPED"
        svc.validate(s, batch_id, emp(s, "HR-001"))
        svc.approve(s, batch_id, emp(s, "HR-002"))
        svc.finalize(s, batch_id, emp(s, "HR-002"))
        statuses = {r.status for r in s.scalars(select(PayrollRun).where(
            PayrollRun.period == "2026-09"))}
        assert statuses == {PayrollRunStatus.LOCKED}  # including the skipped employee

        with pytest.raises(Exception, match="at least 10"):
            svc.reopen_month(s, SEP, emp(s, "HR-001"), "short")
        out = svc.reopen_month(s, SEP, emp(s, "HR-001"), "Correction needed for LOP")
        assert out["unlocked"] >= 1 and svc.get_batch(s, batch_id).stage is None
        assert {r.status for r in s.scalars(select(PayrollRun))} == {PayrollRunStatus.PROCESSED}
        with pytest.raises(svc.BatchConflictError, match="not finalized"):
            svc.reopen_month(s, SEP, emp(s, "HR-001"), "Correction needed again")


def test_month_without_attendance_is_blocked_then_fixed(seeded_engine):
    factory = get_session_factory(seeded_engine)
    month = PayrollPeriod(2026, 10)
    with factory() as s:
        s.execute(delete(Attendance).where(Attendance.day >= month.start))
        s.commit()
        checks = payroll_runs.prechecks(s, month)
        assert not checks["can_run"] and "No attendance is recorded" in checks["blocking"][0]
        hr = emp(s, "HR-001")
        batch = svc.start_review(s, hr, month)  # HR may still try: everyone fails ...
        svc.request_run(s, batch.batch_id, hr, "SKIP_ALL", None)
        s.commit()
        batch_id = batch.batch_id
    BatchWorker(factory, retry_delay=0).run_until_idle()
    with factory() as s:
        row = payroll_runs.register(s, month, search="EMP-101")["items"][0]
        assert row["status"] == "FAILED" and row["error"].startswith("No attendance")  # readable
        made = create_for_month(s, emp(s, "HR-001"), month, "Create October attendance",
                                today=date(2026, 10, 3))  # fmt: skip
        assert made["days"] > 0 and made["up_to"] == "2026-10-03"
        sundays = s.scalars(select(Attendance.status).where(Attendance.day == date(2026, 10, 4)))
        assert not list(sundays)  # nothing after "today"
        checks = payroll_runs.prechecks(s, month)
        assert checks["can_run"]
        assert any("not over yet" in n["message"] for c in checks["checks"] for n in c["notes"])
        svc.retry_failed(s, batch_id, emp(s, "HR-001"))
        s.commit()
    BatchWorker(factory, retry_delay=0).run_until_idle()
    with factory() as s:
        assert payroll_runs.register(s, month, search="EMP-101")["items"][0]["status"] == "PROCESSED"


def test_register_lists_everyone_with_payslip_numbers(seeded_session):
    s = seeded_session
    run = process_payroll(s, emp(s, "EMP-101"), SEP, run_by=emp(s, "HR-001"))
    reg = payroll_runs.register(s, SEP, page_size=100)
    rows = {r["employee_code"]: r for r in reg["items"]}
    assert reg["total"] == len(rows) and rows["EMP-101"]["net"] == str(run.net_pay)
    assert rows["EMP-102"]["status"] == "NOT_PROCESSED" and rows["EMP-102"]["net"] is None

