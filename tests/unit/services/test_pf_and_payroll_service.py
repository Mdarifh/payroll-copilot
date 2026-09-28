from datetime import date
from decimal import Decimal

import pytest

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import UnverifiedRuleError
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.rules.service import RuleService
from payroll_copilot.services.payroll_service import PayrollCalculationService
from payroll_copilot.services.pf_service import PFService, render_comparison, render_pf

SEP, AUG = PayrollPeriod(2026, 9), PayrollPeriod(2026, 8)

EXPECTED_EE_PF_SEP = {
    "ARIF-112": 3000,  # basic 30000 capped at 25000
    "EMP-002": 2160,   # basic 18000 after LOP
    "EMP-003": 1296,
    "EMP-004": 3000,   # 27500 capped
    "EMP-005": 1120,   # joiner basic 9333
    "EMP-006": 1800,   # leaver basic 15000
    "EMP-008": 1378,   # basic 11480
    "EMP-009": 4200,   # PF on actual wage 35000
    "EMP-010": 1726,   # 13920 + LOP-reversal 465
}  # fmt: skip


@pytest.mark.parametrize(("code", "ee_pf"), EXPECTED_EE_PF_SEP.items())
def test_employee_pf_september(seeded_session, code, ee_pf):
    r = PFService(seeded_session).calculate(code, SEP)
    assert r.applicable
    assert r.amounts["EE_PF"] == Decimal(ee_pf)
    assert r.rule.version == 2


def test_excluded_employee(seeded_session):
    r = PFService(seeded_session).calculate("EMP-007", SEP)
    assert not r.applicable and "pf_applicable = false" in r.reason
    assert "not applicable" in render_pf(r, "EMP-007", "2026-09")


def test_august_uses_old_rule(seeded_session):
    r = PFService(seeded_session).calculate("EMP-004", AUG)
    assert (r.rule.version, r.ceiling, r.amounts["EE_PF"]) == (1, Decimal(15000), Decimal(1800))


def test_senior_on_actual_wage_has_no_eps(seeded_session):
    r = PFService(seeded_session).calculate("EMP-009", SEP)
    assert r.amounts["EPS"] == 0 and r.amounts["ER_EPF"] == Decimal(4200)


def test_pf_membership_not_started(seeded_session):
    employee = EmployeeRepository(seeded_session).get_by_code("ARIF-112")
    employee.pf_join_date = date(2026, 10, 1)
    seeded_session.flush()
    r = PFService(seeded_session).calculate("ARIF-112", SEP)
    assert not r.applicable and "starts 2026-10-01" in r.reason


def test_compare_old_and_new_rule(seeded_session):
    rows = PFService(seeded_session).compare_rule_versions("ARIF-112", SEP)
    assert [r.result.rule.version for r in rows] == [1, 2]
    assert [r.is_applicable_rule for r in rows] == [False, True]
    assert rows[0].result.amounts["EE_PF"] == Decimal(1800)
    assert rows[1].result.amounts["EE_PF"] == Decimal(3000)
    text = render_comparison(rows, "ARIF-112", "2026-09")
    assert "Employee PF ₹1,200" in text
    assert PFService(seeded_session).compare_rule_versions("EMP-007", SEP) == []


def test_unverified_rule_can_be_blocked(seeded_session, monkeypatch):
    service = PFService(seeded_session)
    service.rules = RuleService(seeded_session, allow_unverified=False)
    with pytest.raises(UnverifiedRuleError):
        service.calculate("ARIF-112", SEP)


class TestPayroll:
    def test_net_and_ctc_with_pf(self, seeded_session):
        view = PayrollCalculationService(seeded_session).calculate("ARIF-112", SEP)
        s = view.summary
        assert s.gross_earnings == Decimal(60000)
        assert s.total_deductions == Decimal(3200)  # PF 3000 + PT 200
        assert s.net_pay == Decimal(56800)
        assert s.total_employer_contributions == Decimal(3250)
        assert s.monthly_ctc == Decimal(63250)
        assert view.pending_components == []  # TDS ₹0: new regime below rebate limit

    def test_no_pf_employee(self, seeded_session):
        view = PayrollCalculationService(seeded_session).calculate("EMP-007", SEP)
        assert view.summary.net_pay == Decimal(80000 - 200)  # PT only
        assert view.summary.employer_contributions == []
        assert "PF: not deducted" in view.to_text()

    def test_payslip_text(self, seeded_session):
        text = PayrollCalculationService(seeded_session).calculate("EMP-010", SEP).to_text()
        assert "Basic (arrear)" in text
        assert "EMPLOYER CONTRIBUTIONS (not deducted from net pay)" in text
        assert "Income Tax (TDS)" in text and "NOT YET CALCULATED" not in text
