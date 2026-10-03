"""Phase 8: AI payroll audit - mismatches with proven causes, anomalies, health, Fix."""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Employee, PayrollRun
from payroll_copilot.services import pay_adjustments as adj
from payroll_copilot.services import payroll_audit as pa
from payroll_copilot.services.lop_service import set_lop
from payroll_copilot.services.payslip import process_payroll
from tests.unit.chat.conftest import FakeChatModel

SEP = PayrollPeriod(2026, 9)
CODES = ("EMP-101", "EMP-102", "EMP-103", "EMP-104")


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def run_of(s, code) -> PayrollRun:
    return s.scalar(select(PayrollRun).where(PayrollRun.employee_id == emp(s, code).id,
                                             PayrollRun.period == "2026-09"))


def audit_now(s, **filters):
    hr = emp(s, "HR-001")
    row = pa.start(s, hr, SEP, **filters)
    s.commit = s.flush  # keep the test in one transaction
    pa._execute(s, row)
    return row


def by_employee(row, code):
    return [f for f in row.findings if f["employee_code"] == code]


def test_clean_payroll_has_no_findings(seeded_session):
    s = seeded_session
    for c in CODES:
        process_payroll(s, emp(s, c), SEP, run_by=emp(s, "HR-001"))
    row = audit_now(s, department=emp(s, "EMP-101").department)
    assert row.findings == [] and row.summary["payslips_checked"] >= 1


def test_changes_after_processing_are_found_with_their_cause(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    for c in CODES:
        process_payroll(s, emp(s, c), SEP, run_by=hr)
    set_lop(s, hr, emp(s, "EMP-101"), SEP, Decimal(1), "absent on the 15th")
    adj.add_overtime(s, hr, emp(s, "EMP-102"), date(2026, 9, 12), "3", "weekend release")
    row = audit_now(s)
    lop = by_employee(row, "EMP-101")
    assert any(f["type"] == "LOP" and "LOP was changed by Anjali Mehta after payroll was "
               "processed" in f["causes"][0] for f in lop)
    ot = by_employee(row, "EMP-102")
    assert any(f["component"] == "OVERTIME" and "Overtime entries changed" in f["causes"][0]
               for f in ot)
    assert not by_employee(row, "EMP-103")  # untouched employee stays clean
    assert row.summary["failed"] + row.summary["warnings"] >= 2


def test_pf_with_the_wrong_rule_version_is_proven(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    process_payroll(s, emp(s, "EMP-104"), SEP, run_by=hr)  # basic above the ceilings
    run = run_of(s, "EMP-104")
    slip = dict(run.payslip)
    good = next(Decimal(ln["amount"]) for ln in slip["deductions"] if ln["head_code"] == "EE_PF")
    wrong = Decimal(3000)  # 12% of the new ₹25,000 ceiling for the whole month
    slip["deductions"] = [{**ln, "amount": str(wrong)} if ln["head_code"] == "EE_PF" else ln
                          for ln in slip["deductions"]]  # fmt: skip
    run.payslip = slip
    run.total_deductions += wrong - good
    run.net_pay -= wrong - good
    s.flush()
    row = audit_now(s)
    pf = [f for f in by_employee(row, "EMP-104") if f["issue"] == "PF mismatch"]
    assert pf and pf[0]["type"] == "RULE_VERSION" and pf[0]["severity"] == "HIGH"
    assert "EPF v2 ceiling was applied to the whole month" in pf[0]["causes"][0]
    assert Decimal(pf[0]["expected"]) == good and Decimal(pf[0]["actual"]) == wrong
    assert row.summary["statutory_impact"]["PF"] == str(wrong - good)
    pf_health = next(c for c in row.health["categories"] if c["category"] == "PF")
    assert pf_health["status"] == "FAIL" and pf_health["failed"] == 1


def test_missing_payslip_and_undecided_absence_are_anomalies(seeded_session):
    s = seeded_session
    process_payroll(s, emp(s, "EMP-101"), SEP, run_by=emp(s, "HR-001"))
    row = audit_now(s)
    missing = [a for a in row.anomalies if a["issue"] == "Missing payslip"]
    assert any(a["employee_code"] == "EMP-102" for a in missing)
    assert 0 < row.health["score"] < 100


def test_background_audit_and_fix_over_http(seeded_engine, make_settings):
    from payroll_copilot.db.session import get_session_factory

    factory = get_session_factory(seeded_engine)
    with factory() as s:
        hr = emp(s, "HR-001")
        process_payroll(s, emp(s, "EMP-101"), SEP, run_by=hr)
        adj.add_adjustment(s, hr, emp(s, "EMP-101"), SEP, "EARNING", "BONUS", "900", "spot award")
        s.commit()
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip

    def auth(email, password):
        token = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    hr, employee = auth("anjali.mehta@example.com", "Hr@12345"), auth(
        "rahul.verma@example.com", "Employee@123")
    assert client.post("/audits", json={"month": "2026-09"}, headers=employee).status_code == 403
    started = client.post("/audits", json={"month": "2026-09"}, headers=hr).json()
    done = client.get(f"/audits/{started['id']}", headers=hr).json()  # background task ran
    assert done["status"] == "COMPLETED"
    assert any(f["employee_code"] == "EMP-101" and f["can_fix"] for f in done["findings"])
    fix = client.post("/payroll-runs/2026-09/employees/EMP-101/recalculate",
                      json={"reason": "Audit finding: bonus missing"}, headers=hr).json()
    assert Decimal(fix["new_net"]) - Decimal(fix["old_net"]) == 900
    again = client.post("/audits", json={"month": "2026-09"}, headers=hr).json()
    clean = client.get(f"/audits/{again['id']}", headers=hr).json()
    assert not any(f["employee_code"] == "EMP-101" for f in clean["findings"])
    assert client.get("/audits/latest?month=2026-09", headers=hr).json()["id"] == again["id"]
