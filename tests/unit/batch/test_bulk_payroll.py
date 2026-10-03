"""Bulk payroll: review, LOP edits, run-once, worker reliability, restart, scale."""

import threading
import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.base import Base
from payroll_copilot.db.models import (
    Attendance,
    AuditLog,
    BatchItem,
    Employee,
    LopRecord,
    PayrollRun,
    SalaryStructure,
)
from payroll_copilot.db.session import create_db_engine, get_session_factory
from payroll_copilot.domain.enums import Role
from payroll_copilot.services import batch_service as svc
from payroll_copilot.services.batch_worker import BatchWorker

SEP = PayrollPeriod(2026, 9)


# ---------------------------------------------------------------------- helpers


def hr_of(session) -> Employee:
    return session.scalar(select(Employee).where(Employee.role == Role.HR))


def fake_processor(calls: list | None = None, fail_for: set[str] = frozenset(),
                   crash_after: int | None = None):  # fmt: skip
    """Writes a minimal payroll run (so idempotency checks are real) and records calls."""
    seen = calls if calls is not None else []

    def process(session, employee, period, run_by, batch_id):
        if crash_after is not None and len(seen) >= crash_after:
            raise KeyboardInterrupt("server killed")  # not an Exception: like a hard stop
        if employee.employee_code in fail_for:
            raise ValueError(f"salary structure missing for {employee.employee_code}")
        session.add(PayrollRun(employee_id=employee.id, period=str(period), paid_days=30,
                               lop_days=0, gross_earnings=1000, total_deductions=0,
                               net_pay=1000, payslip={}, run_by_id=run_by.id))  # fmt: skip
        session.flush()
        seen.append(employee.employee_code)
        return Decimal(1000)

    return process


def make_company(engine, count: int, exception_every: int = 50,
                 departments=("Sales", "Engineering")) -> None:  # fmt: skip
    """Fast bulk insert: ``count`` employees, every ``exception_every``-th has 2 absences."""
    with get_session_factory(engine)() as s:
        s.execute(insert(Employee), [{
            "employee_code": f"E-{i:05d}", "full_name": f"Person {i}", "gender": "M",
            "dob": date(1990, 1, 1), "doj": date(2020, 1, 1), "work_state": "MH",
            "department": departments[i % len(departments)],
            "email": f"p{i}@example.com", "password_hash": "x",
            "role": "hr" if i == 0 else "employee",
        } for i in range(count)])  # fmt: skip
        ids = s.scalars(select(Employee.id).order_by(Employee.id)).all()
        exceptions = ids[1::exception_every]
        s.execute(insert(SalaryStructure), [
            {"employee_id": e, "effective_from": date(2026, 4, 1), "head_code": head,
             "monthly_amount": amount}
            for e in exceptions for head, amount in (("BASIC", 18000), ("HRA", 9000),
                                                     ("SPECIAL", 3000))])  # fmt: skip
        s.execute(insert(Attendance), [
            {"employee_id": e, "day": day, "status": "ABSENT"}
            for e in exceptions for day in (date(2026, 9, 22), date(2026, 9, 23))])
        s.commit()


@pytest.fixture
def big_engine(tmp_path):
    """A file DB (real locking between threads), empty schema."""
    import payroll_copilot.db.models  # noqa: F401

    engine = create_db_engine(f"sqlite:///{(tmp_path / 'bulk.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def start(engine, **filters):
    with get_session_factory(engine)() as s:
        batch = svc.start_review(s, hr_of(s), SEP, **filters)
        s.commit()
        return batch.batch_id


def run(engine, batch_id, mode="APPLY", reason="Unapproved absence in September"):
    with get_session_factory(engine)() as s:
        svc.request_run(s, batch_id, hr_of(s), mode, reason)
        s.commit()


def progress(engine, batch_id) -> dict:
    with get_session_factory(engine)() as s:
        return svc.status(s, svc.get_batch(s, batch_id))


# ---------------------------------------------------------------------- real engine


def test_end_to_end_with_the_real_payroll_engine(seeded_engine):
    factory = get_session_factory(seeded_engine)
    batch_id = start(seeded_engine)
    with factory() as s:
        info = svc.summary(svc.get_batch(s, batch_id))
        assert (info["total"], info["review_count"], info["ok_count"]) == (12, 2, 10)  # 12 named people incl. HR-002, MGR-001, intern, 3 staff
        items = svc.lop_items(s, svc.get_batch(s, batch_id))["items"]
        assert [i["emp_id"] for i in items] == ["EMP-101", "EMP-102"]
        priya = items[1]
        assert priya["absent_dates"] == ["2026-09-24", "2026-09-25"]
        assert (priya["per_day"], priya["deduction"]) == ("1333.33", "2667")  # 40000/30 × 2
        # HR skips Rahul, keeps Priya's 2 days
        svc.update_lop_item(s, svc.get_batch(s, batch_id), "EMP-101", False, None)
        totals = svc.lop_items(s, svc.get_batch(s, batch_id))["totals"]
        assert totals == {"selected": 1, "total_lop_days": "2", "total_deduction": "2667"}
        s.commit()

    run(seeded_engine, batch_id)
    BatchWorker(factory, retry_delay=0).run_until_idle()

    done = progress(seeded_engine, batch_id)
    assert done["status"] == "COMPLETED" and done["percent"] == 100
    assert (done["done"], done["failed"], done["lop_applied"]) == (12, 0, 1)
    with factory() as s:
        def run_of(code):
            emp = s.scalar(select(Employee).where(Employee.employee_code == code))
            return s.scalar(select(PayrollRun).where(PayrollRun.employee_id == emp.id,
                                                     PayrollRun.period == "2026-09"))
        assert run_of("EMP-102").lop_days == 2 and run_of("EMP-102").gross_earnings == 37333
        assert run_of("EMP-101").lop_days == 0
        # PF split at 17-Sep (S.O. 5109(E)); Priya's LOP on 24-25 Sep falls in the NEW part:
        # OLD 1-16: min(20000x16/30, 15000x16/30) = 8000; NEW 17-30: 20000x12/30 = 8000
        priya = run_of("EMP-102").payslip
        assert [(s["start"], s["end"], s["rule_version"], s["lop_days"]) for s in
                priya["pf_segments"]] == [("2026-09-01", "2026-09-16", "EPF v1", "0"),
                                          ("2026-09-17", "2026-09-30", "EPF v2", "2")]
        ee = next(d for d in priya["deductions"] if d["head_code"] == "EE_PF")
        assert ee["amount"] == "1920"
        assert priya["pf_details"]["lop_dates"] == {"2026-09-24": "1", "2026-09-25": "1"}
        from payroll_copilot.db.models import PfCalculationAudit
        row = s.scalars(select(PfCalculationAudit).where(
            PfCalculationAudit.batch_id == batch_id)).all()
        assert len(row) == 12 and {r.status for r in row} == {"OK", "NOT_APPLICABLE"}
        lop = s.scalar(select(LopRecord))
        assert (lop.days, lop.reason) == (2, "Unapproved absence in September")
        audit = s.scalars(select(AuditLog).where(AuditLog.request_id == batch_id)).all()
        actions = [a.action for a in audit]
        assert actions.count("lop.update") == 1 and actions.count("payroll.run") == 12
        assert all(a.new_value.get("batch_id") == batch_id for a in audit
                   if a.action in ("lop.update", "payroll.run"))  # fmt: skip


def test_reason_required_and_skip_all(seeded_engine):
    batch_id = start(seeded_engine)
    with pytest.raises(svc.PayrollCopilotError, match="reason"):
        run(seeded_engine, batch_id, reason="  ")
    run(seeded_engine, batch_id, mode="SKIP_ALL", reason=None)  # no LOP -> no reason needed
    with get_session_factory(seeded_engine)() as s:
        assert s.scalar(select(func.count()).select_from(LopRecord)) == 0


def test_lop_edits_are_validated_and_locked_after_run(seeded_engine):
    batch_id = start(seeded_engine)
    with get_session_factory(seeded_engine)() as s:
        batch = svc.get_batch(s, batch_id)
        for bad in (Decimal(3), Decimal("-1"), Decimal("0.3")):  # Priya was absent 2 days
            with pytest.raises(svc.PayrollCopilotError, match="between 0 and 2"):
                svc.update_lop_item(s, batch, "EMP-102", None, bad)
        row = svc.update_lop_item(s, batch, "EMP-102", None, Decimal("1.5"))["item"]
        assert (row["lop_days"], row["deduction"]) == ("1.5", "2000")
        with pytest.raises(svc.NotFoundError):
            svc.update_lop_item(s, batch, "EMP-104", True, None)  # no exceptions
        s.commit()
    run(seeded_engine, batch_id)
    with get_session_factory(seeded_engine)() as s, pytest.raises(svc.BatchConflictError):
        svc.update_lop_item(s, svc.get_batch(s, batch_id), "EMP-102", None, Decimal(1))


# ---------------------------------------------------------------------- spec tests


def test_5000_employees_with_2_percent_exceptions(big_engine):
    make_company(big_engine, 5000, exception_every=50)  # 100 exceptions = 2%
    started = time.perf_counter()
    batch_id = start(big_engine)
    review_seconds = time.perf_counter() - started
    info = progress(big_engine, batch_id)
    assert (info["total"], info["review_count"], info["ok_count"]) == (5000, 100, 4900)
    with get_session_factory(big_engine)() as s:
        page = svc.lop_items(s, svc.get_batch(s, batch_id), page=3, page_size=20, search="person")
        assert page["total_items"] == 100 and page["pages"] == 5 and len(page["items"]) == 20
        # total for the WHOLE batch: 100 × 2 days × (30000 / 30) = 2,00,000
        assert page["totals"]["total_deduction"] == "200000"

    run(big_engine, batch_id)
    calls: list = []
    BatchWorker(get_session_factory(big_engine), processor=fake_processor(calls),
                chunk_size=500, retry_delay=0).run_until_idle()
    done = progress(big_engine, batch_id)
    assert (done["done"], done["failed"], done["lop_applied"], done["status"]) == (
        5000, 0, 100, "COMPLETED")
    assert len(calls) == len(set(calls)) == 5000
    assert review_seconds < 30, f"review of 5000 employees took {review_seconds:.1f}s"


def test_one_employee_failing_does_not_stop_the_batch_and_retry_failed(big_engine):
    make_company(big_engine, 60)
    batch_id = start(big_engine)
    run(big_engine, batch_id)
    factory = get_session_factory(big_engine)
    BatchWorker(factory, processor=fake_processor(fail_for={"E-00007"}), chunk_size=10,
                retry_delay=0).run_until_idle()  # fmt: skip
    done = progress(big_engine, batch_id)
    assert (done["status"], done["done"], done["failed"]) == ("COMPLETED_WITH_ERRORS", 59, 1)
    assert done["can_retry_failed"]
    with factory() as s:
        report = svc.report_csv(s, svc.get_batch(s, batch_id))
        failed_line = next(line for line in report.splitlines() if "E-00007" in line)
        assert "FAILED" in failed_line and "salary structure missing" in failed_line

        hr = hr_of(s)
        svc.retry_failed(s, batch_id, hr)
        s.commit()
    calls: list = []
    BatchWorker(factory, processor=fake_processor(calls), retry_delay=0).run_until_idle()
    assert calls == ["E-00007"]  # only the failed one ran again
    again = progress(big_engine, batch_id)
    assert (again["status"], again["done"], again["failed"]) == ("COMPLETED", 60, 0)
    with factory() as s, pytest.raises(svc.BatchConflictError):
        svc.retry_failed(s, batch_id, hr_of(s))  # nothing left to retry


def test_double_click_on_run_runs_payroll_once(big_engine):
    make_company(big_engine, 20)
    batch_id = start(big_engine)
    results: list = []
    barrier = threading.Barrier(5)

    def click():
        barrier.wait()
        try:
            run(big_engine, batch_id)
            results.append("started")
        except svc.BatchConflictError:
            results.append("conflict")

    threads = [threading.Thread(target=click) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["conflict"] * 4 + ["started"]
    with get_session_factory(big_engine)() as s:
        assert svc.get_batch(s, batch_id).runs == 1
        runs = s.scalars(select(AuditLog).where(AuditLog.action == "batch.run")).all()
        assert len(runs) == 1


def test_filter_by_department_and_state(big_engine):
    make_company(big_engine, 40, departments=("Sales", "Engineering", "Finance", "Ops"))
    batch_id = start(big_engine, department="sales")
    with get_session_factory(big_engine)() as s:
        batch = svc.get_batch(s, batch_id)
        assert batch.total == 10 and batch.filters == {"department": "sales"}
        codes = s.scalars(select(Employee.department).join(
            BatchItem, BatchItem.employee_id == Employee.id).where(
            BatchItem.batch_pk == batch.id)).all()  # fmt: skip
        assert set(codes) == {"Sales"}
    assert progress(big_engine, start(big_engine, work_state="Mumbai office"))["total"] == 40
    assert progress(big_engine, start(big_engine, work_state="Delhi"))["total"] == 0
    with pytest.raises(svc.PayrollCopilotError, match="Unknown state"):
        start(big_engine, work_state="Atlantis")


def test_server_restart_mid_batch_processes_everyone_exactly_once(big_engine):
    make_company(big_engine, 100)
    batch_id = start(big_engine)
    run(big_engine, batch_id)
    factory = get_session_factory(big_engine)
    first: list = []
    with pytest.raises(KeyboardInterrupt):  # the "server" dies in the middle of employee 38
        BatchWorker(factory, processor=fake_processor(first, crash_after=37),
                    chunk_size=10, retry_delay=0).run_until_idle()  # fmt: skip
    mid = progress(big_engine, batch_id)
    assert mid["status"] == "RUNNING" and mid["done"] == 37 and mid["pending"] == 63

    second: list = []  # restart: a new worker resumes the RUNNING batch
    BatchWorker(factory, processor=fake_processor(second), retry_delay=0).run_until_idle()
    assert not set(first) & set(second) and len(first) + len(second) == 100
    done = progress(big_engine, batch_id)
    assert (done["status"], done["done"]) == ("COMPLETED", 100)
    with factory() as s:
        runs = s.scalar(select(func.count()).select_from(PayrollRun))
        assert runs == 100  # nobody paid twice


def test_already_processed_employees_are_skipped(seeded_engine):
    batch_id = start(seeded_engine, work_state="MH")  # EMP-101, HR-001, HR-002
    run(seeded_engine, batch_id)
    BatchWorker(get_session_factory(seeded_engine), retry_delay=0).run_until_idle()
    again = start(seeded_engine, work_state="MH")  # same month, same people
    info = progress(seeded_engine, again)
    assert (info["total"], info["already_processed"], info["review_count"]) == (3, 3, 0)
    run(seeded_engine, again, mode="SKIP_ALL", reason=None)
    BatchWorker(get_session_factory(seeded_engine), retry_delay=0).run_until_idle()
    final = progress(seeded_engine, again)
    assert (final["skipped"], final["done"], final["status"]) == (3, 0, "COMPLETED")


def test_temporary_errors_are_retried(big_engine):
    from payroll_copilot.services.reliability import TransientError

    make_company(big_engine, 3)
    batch_id = start(big_engine)
    run(big_engine, batch_id)
    attempts: dict[str, int] = {}
    inner = fake_processor()

    def flaky(session, employee, period, run_by, bid):
        attempts[employee.employee_code] = attempts.get(employee.employee_code, 0) + 1
        if attempts[employee.employee_code] < 3:
            raise TransientError("payroll API timeout")
        return inner(session, employee, period, run_by, bid)

    BatchWorker(get_session_factory(big_engine), processor=flaky, retry_delay=0).run_until_idle()
    assert progress(big_engine, batch_id)["done"] == 3
    assert set(attempts.values()) == {3}  # failed twice, succeeded on the third try
