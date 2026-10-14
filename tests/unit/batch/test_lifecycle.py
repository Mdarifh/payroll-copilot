"""Payroll Runs lifecycle: calculate -> validate -> approve (maker-checker) -> finalize,
rollback, blocking errors, coverage warnings and the EPF challan."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PermissionDeniedError
from payroll_copilot.db.models import Employee, PayrollRun
from payroll_copilot.db.session import get_session_factory
from payroll_copilot.domain.enums import PayrollRunStatus
from payroll_copilot.services import batch_service as svc
from payroll_copilot.services.batch_worker import BatchWorker
from payroll_copilot.services.payslip import PayrollLockedError, process_payroll
from tests.unit.chat.conftest import FakeChatModel

SEP = PayrollPeriod(2026, 9)


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def calculated_batch(engine, **filters) -> str:
    """Review + run (SKIP_ALL) + worker, prepared by HR-001."""
    factory = get_session_factory(engine)
    with factory() as s:
        batch = svc.start_review(s, emp(s, "HR-001"), SEP, **filters)
        svc.request_run(s, batch.batch_id, emp(s, "HR-001"), "SKIP_ALL", None)
        s.commit()
        batch_id = batch.batch_id
    BatchWorker(factory, retry_delay=0).run_until_idle()
    return batch_id


def test_full_lifecycle_with_maker_checker(seeded_engine):
    batch_id = calculated_batch(seeded_engine)
    factory = get_session_factory(seeded_engine)
    with factory() as s:
        result = svc.validate(s, batch_id, emp(s, "HR-001"))
        assert result["stage"] == "VALIDATED" and result["errors"] == 0
        checks = svc.validation_results(s, svc.get_batch(s, batch_id), page_size=500)["items"]
        codes = {c["code"] for c in checks}
        assert {"PF_RULE_CONFIG", "PF_TRANSITION", "PF_TRANSITION_CASE", "PF_CALCULATED"} <= codes
        intern = next(c for c in checks if c["employee_code"] == "INT-001")
        assert intern["severity"] == "PASS" and "PF not applicable" in intern["message"]

        with pytest.raises(PermissionDeniedError, match="different HR user"):
            svc.approve(s, batch_id, emp(s, "HR-001"))  # the preparer cannot approve
        svc.approve(s, batch_id, emp(s, "HR-002"))
        svc.finalize(s, batch_id, emp(s, "HR-002"))
        s.commit()
        runs = s.scalars(select(PayrollRun).where(PayrollRun.period == "2026-09")).all()
        assert runs and {r.status for r in runs} == {PayrollRunStatus.LOCKED}
        with pytest.raises(PayrollLockedError):  # J05: a finalized month cannot be recalculated
            process_payroll(s, emp(s, "EMP-101"), SEP, run_by=emp(s, "HR-001"))
        with pytest.raises(svc.BatchConflictError, match="cannot be rolled back"):
            svc.rollback(s, batch_id, emp(s, "HR-001"), "trying to undo")


def test_rollback_then_recalculate_gives_the_same_result(seeded_engine):
    factory = get_session_factory(seeded_engine)
    batch_id = calculated_batch(seeded_engine, work_state="KA")

    def net_pay():
        with factory() as s:
            return {r.employee_id: r.net_pay for r in s.scalars(
                select(PayrollRun).where(PayrollRun.period == "2026-09"))}

    first = net_pay()
    with factory() as s:
        batch = svc.rollback(s, batch_id, emp(s, "HR-001"), "LOP correction needed")
        assert batch.status.value == "REVIEW" and batch.stage is None
        s.commit()
    assert net_pay() == {}
    with factory() as s:
        svc.request_run(s, batch_id, emp(s, "HR-001"), "SKIP_ALL", None)
        s.commit()
    BatchWorker(factory, retry_delay=0).run_until_idle()
    assert net_pay() == first  # J02: recalculation equals the first result


def test_missing_pf_date_blocks_approval(seeded_engine):
    factory = get_session_factory(seeded_engine)
    with factory() as s:
        emp(s, "EMP-104").pf_applicable_date = None
        s.commit()
    batch_id = calculated_batch(seeded_engine, work_state="TN")
    with factory() as s:
        result = svc.validate(s, batch_id, emp(s, "HR-001"))
        assert result["stage"] == "VALIDATION_FAILED" and result["errors"] >= 1
        errors = svc.validation_results(s, svc.get_batch(s, batch_id), "error")["items"]
        assert any("PF Applicable Date is missing" in e["message"] for e in errors)
        with pytest.raises(svc.BatchConflictError, match="validated payroll without errors"):
            svc.approve(s, batch_id, emp(s, "HR-002"))


def test_coverage_and_duplicate_warnings(seeded_engine):
    factory = get_session_factory(seeded_engine)
    with factory() as s:
        rohan = emp(s, "EMP-103")  # ₹18,000 LOW structure, PF wage 10,800 <= 25,000
        rohan.pf_applicable, rohan.pf_exempt_reason = False, "Opted out (test)"
        emp(s, "EMP-102").uan = emp(s, "EMP-101").uan
        s.commit()
    batch_id = calculated_batch(seeded_engine)
    with factory() as s:
        svc.validate(s, batch_id, emp(s, "HR-001"))
        items = svc.validation_results(s, svc.get_batch(s, batch_id), page_size=500)["items"]
        coverage = [i for i in items if i["code"] == "PF_COVERAGE_REQUIRED"]
        assert [c["employee_code"] for c in coverage] == ["EMP-103"]
        assert "from 2026-09-17" in coverage[0]["message"]
        dup = [i for i in items if i["code"] == "DUPLICATE_EMPLOYEE"]
        assert dup and "EMP-101" in dup[0]["message"] and "EMP-102" in dup[0]["message"]


def test_challan_applies_the_establishment_minimum_once(seeded_engine):
    calculated_batch(seeded_engine, work_state="WB")  # only Rohan (₹18,000 structure)
    with get_session_factory(seeded_engine)() as s:
        challan = svc.pf_challan(s, SEP)
    assert challan["contributing_members"] == 1
    assert float(challan["admin_by_employees"]) < 500
    assert challan["admin_payable"] == "500" and challan["admin_minimum_applied"]
    accounts = {a["account"]: a["amount"] for a in challan["accounts"]}
    assert accounts["A/c 2"] == "500" and accounts["A/c 22"] == "0"


def test_lifecycle_over_http(seeded_engine, make_settings):
    batch_id = calculated_batch(seeded_engine, work_state="MH")
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip

    def auth(email, password="Hr@12345"):
        token = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    maker, checker = auth("anjali.mehta@example.com"), auth("rohit.kapoor@example.com")
    employee = auth("rahul.verma@example.com", "Employee@123")
    assert client.post(f"/batches/{batch_id}/validate", headers=employee).status_code == 403
    assert client.post(f"/batches/{batch_id}/validate", headers=maker).json()["errors"] == 0
    page = client.get(f"/batches/{batch_id}/validation?severity=warning", headers=maker).json()
    assert page["items"] and {i["severity"] for i in page["items"]} == {"WARNING"}
    assert client.post(f"/batches/{batch_id}/approve", headers=maker).status_code == 403
    assert client.post(f"/batches/{batch_id}/approve", headers=checker).json()["stage"] == (
        "APPROVED")
    assert client.post(f"/batches/{batch_id}/finalize", headers=checker).json()["stage"] == (
        "FINALIZED")
    challan = client.get("/payroll-runs/2026-09/challan", headers=maker).json()
    assert challan["contributing_members"] == 3 and date.fromisoformat("2026-09-01")


def test_month_view_shows_the_runs_own_pay_and_who_may_approve(seeded_engine):
    from decimal import Decimal

    from payroll_copilot.services.payroll_runs import month_detail

    calculated_batch(seeded_engine, department="Sales")  # another run of the same month
    batch_id = calculated_batch(seeded_engine, department="Engineering")
    factory = get_session_factory(seeded_engine)
    with factory() as s:
        svc.validate(s, batch_id, emp(s, "HR-001"))
        s.flush()
        maker = month_detail(s, SEP, emp(s, "HR-001"))
        run = next(b for b in maker["batches"] if b["batch_id"] == batch_id)
        mine = s.scalars(select(PayrollRun.net_pay).join(
            Employee, Employee.id == PayrollRun.employee_id).where(
            PayrollRun.period == "2026-09", Employee.department == "Engineering")).all()
        assert Decimal(run["batch_net"]) == sum(mine) < Decimal(maker["net"])
        assert run["prepared_by_me"] and not run["can_approve"]
        assert "different HR user" in run["approve_blocked_reason"]
        checker = next(b for b in month_detail(s, SEP, emp(s, "HR-002"))["batches"]
                       if b["batch_id"] == batch_id)
        assert checker["can_approve"] and checker["approve_blocked_reason"] is None
