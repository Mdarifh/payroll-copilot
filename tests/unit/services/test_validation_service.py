"""Expected vs actual on the demo actual payroll (deliberate, documented errors)."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from payroll_copilot.calculations.validation import IssueType
from payroll_copilot.core.config import get_settings
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Payroll, PayrollDeduction
from payroll_copilot.domain.enums import ComparisonStatus as S
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.services.actual_payroll_seed import seed_actual_payroll
from payroll_copilot.services.validation_service import ValidationService

SEP = PayrollPeriod(2026, 9)
D = Decimal


@pytest.fixture
def actual_session(seeded_session):
    report = seed_actual_payroll(seeded_session)
    assert report.created and report.employees == 10
    seeded_session.flush()
    return seeded_session


def validate(session, code):
    v = ValidationService(session).validate_employee(code, SEP)
    return v, {r.component: r for r in v.results}


def test_seed_actual_is_idempotent_and_replaceable(actual_session):
    assert not seed_actual_payroll(actual_session).created
    again = seed_actual_payroll(actual_session, replace=True)
    assert again.created and len(again.injected) == 7


@pytest.mark.parametrize("code", ["EMP-003", "EMP-006", "EMP-009"])
def test_correct_payslips_have_no_issues(actual_session, code):
    v, _ = validate(actual_session, code)
    assert v.issues == []


def test_old_pf_ceiling_detected(actual_session):
    _, r = validate(actual_session, "ARIF-112")
    pf = r["EE_PF"]
    assert (pf.expected, pf.actual, pf.difference, pf.status) == (D(3000), D(1800), D(-1200), S.MISMATCH)
    assert pf.issue_type is IssueType.RULE_VERSION
    assert "EPF v1" in pf.root_causes[0] and "15000" in pf.root_causes[0]
    assert pf.expected_trace.rule.version == 2
    assert r["EPS"].issue_type is IssueType.RULE_VERSION
    assert "EE_PF +1200" in r["NET_PAY"].root_causes[0]
    assert r["TOTAL_DEDUCTIONS"].root_causes[0] == "Sum of component differences: EE_PF -1200"


def test_lop_not_deducted(actual_session):
    _, r = validate(actual_session, "EMP-002")
    assert r["PAID_DAYS"].issue_type is IssueType.LOP
    assert "LOP of 3 days not deducted" in r["PAID_DAYS"].root_causes[0]
    assert r["GROSS"].issue_type is IssueType.LOP
    assert r["BASIC"].issue_type is IssueType.PRORATION
    assert r["EE_PF"].issue_type is IssueType.UPSTREAM  # PF right for the wrong wage


def test_joiner_paid_full_month(actual_session):
    _, r = validate(actual_session, "EMP-005")
    assert "14 days before DOJ" in r["PAID_DAYS"].root_causes[0]
    assert r["GROSS"].issue_type is IssueType.PRORATION
    assert r["EE_PF"].issue_type is IssueType.UPSTREAM


def test_arrear_not_paid(actual_session):
    _, r = validate(actual_session, "EMP-004")
    assert (r["ARREARS"].expected, r["ARREARS"].actual) == (D(5000), D(0))
    assert r["ARREARS"].issue_type is IssueType.ARREAR
    assert r["BASIC"].root_causes == ["BASIC arrear of ₹2500 missing in actual"]
    assert r["EE_PF"].status is S.MATCH  # Aug arrear had no PF impact


def test_lop_reversal_not_paid(actual_session):
    _, r = validate(actual_session, "EMP-010")
    assert "LOP reversal of ₹775 not paid" in r["ARREARS"].root_causes[0]
    assert r["BASIC"].root_causes == ["BASIC LOP reversal of ₹465 missing in actual"]


def test_esic_rounding_and_pt_slab(actual_session):
    _, r = validate(actual_session, "EMP-008")
    assert r["EE_ESIC"].issue_type is IssueType.ROUNDING
    assert "rounded HALF_UP; rule rounding is CEIL" in r["EE_ESIC"].root_causes[0]
    assert r["PT"].issue_type is IssueType.SLAB
    assert "20000<wage≤∞ slab" in r["PT"].root_causes[0]


def test_header_inconsistency(actual_session):
    v, r = validate(actual_session, "EMP-007")
    assert {i.component for i in v.issues} == {"HEADER_NET", "NET_PAY"}
    assert r["HEADER_NET"].issue_type is IssueType.DATA_QUALITY
    assert r["NET_PAY"].issue_type is IssueType.DATA_QUALITY


def test_period_summary(actual_session):
    result = ValidationService(actual_session).validate_period(SEP)
    assert len(result.employees) == 10
    assert sum(1 for e in result.employees if e.issues) == 7
    types = result.by_issue_type
    for kind in ("RULE_VERSION", "LOP", "PRORATION", "ARREAR", "ROUNDING", "SLAB", "DATA_QUALITY"):
        assert types.get(kind), kind
    text = result.to_text()
    assert "with issues: 7" in text and "[OK ] EMP-003" in text


def test_tolerance(actual_session, monkeypatch):
    monkeypatch.setenv("COMPARISON_TOLERANCE", "1")
    get_settings.cache_clear()
    _, r = validate(actual_session, "EMP-008")
    assert r["EE_ESIC"].status is S.WITHIN_TOLERANCE and not r["EE_ESIC"].is_issue


def test_unexpected_deduction(actual_session):
    employee = EmployeeRepository(actual_session).get_by_code("EMP-003")
    slip = actual_session.scalar(select(Payroll).where(Payroll.employee_id == employee.id))
    actual_session.add(PayrollDeduction(payroll_id=slip.id, head_code="LOAN", amount=D(500)))
    actual_session.flush()
    _, r = validate(actual_session, "EMP-003")
    assert r["LOAN"].status is S.MISSING_EXPECTED
    assert r["LOAN"].issue_type is IssueType.ELIGIBILITY


def test_missing_payslip(actual_session):
    employee = EmployeeRepository(actual_session).get_by_code("EMP-009")
    slip = actual_session.scalar(select(Payroll).where(Payroll.employee_id == employee.id))
    actual_session.delete(slip)
    actual_session.flush()
    v, r = validate(actual_session, "EMP-009")
    assert r["PAYSLIP"].status is S.MISSING_ACTUAL and v.note == "No actual payslip found"


def test_no_actual_run(seeded_session):
    result = ValidationService(seeded_session).validate_period(SEP)
    assert all(e.note == "No actual payslip found" for e in result.employees)
