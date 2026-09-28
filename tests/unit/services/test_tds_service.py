from decimal import Decimal

import pytest

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Payroll, PayrollDeduction, PayrollRun
from payroll_copilot.domain.enums import PayrollSource
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.services.payroll_service import PayrollCalculationService
from payroll_copilot.services.tds_service import TDSService, render_tds

SEP = PayrollPeriod(2026, 9)
D = Decimal


def test_senior_old_regime_with_declarations(seeded_session):
    r = TDSService(seeded_session).calculate("EMP-009", SEP)
    assert r.rule.rule_code == "TDS-OLD-SENIOR"
    assert r.gross_taxable_salary == D(840000)
    assert r.section_deductions == {"80C": D(150000), "80D": D(50000)}
    assert r.annual.total_tax == D(29120)
    assert r.tds_already_deducted == D(12133)  # even spread, 5 of 12 months
    assert (r.remaining_months, r.monthly_tds) == (7, D(2427))


@pytest.mark.parametrize("code", ["ARIF-112", "EMP-002", "EMP-007"])
def test_new_regime_below_rebate_limit_is_zero(seeded_session, code):
    r = TDSService(seeded_session).calculate(code, SEP)
    assert r.rule.rule_code == "TDS-NEW" and r.monthly_tds == 0
    assert not any("PT" in w for w in r.warnings)  # PT not deductible, not even projected


def test_joiner_projects_only_employed_months(seeded_session):
    r = TDSService(seeded_session).calculate("EMP-005", SEP)
    assert len(r.trace.inputs and r.trace.steps[0].values) == 7  # Sep..Mar
    assert r.remaining_months == 7 and r.tds_already_deducted == 0
    assert any("Form 12B" in w for w in r.warnings)


def test_leaver_final_month(seeded_session):
    r = TDSService(seeded_session).calculate("EMP-006", SEP)
    assert r.remaining_months == 1
    assert any("balance tax for the FY recovered now" in w for w in r.warnings)


def test_actual_history_is_used_when_present(seeded_session):
    employee = EmployeeRepository(seeded_session).get_by_code("EMP-009")
    for month, amount in (("2026-04", 2000), ("2026-05", 2000)):
        run = PayrollRun(period=month, source=PayrollSource.ACTUAL)
        slip = Payroll(run=run, employee_id=employee.id, period=month, paid_days=30, lop_days=0,
                       gross_earnings=70000, total_deductions=amount, net_pay=70000 - amount,
                       total_employer_contribution=0)  # fmt: skip
        slip.deductions = [PayrollDeduction(head_code="TDS", amount=D(amount))]
        seeded_session.add(slip)
    seeded_session.flush()
    r = TDSService(seeded_session).calculate("EMP-009", SEP)
    assert r.tds_already_deducted == D(4000)
    assert any("No actual payroll TDS for ['2026-06', '2026-07', '2026-08']" in w
               for w in r.warnings)  # fmt: skip
    assert r.monthly_tds == D(round((29120 - 4000) / 7))


def test_override_and_render(seeded_session):
    r = TDSService(seeded_session).calculate("EMP-009", SEP, D(3000), "employee request")
    text = render_tds(r, "EMP-009", "2026-09")
    assert "₹3,000 (OVERRIDDEN)" in text and "computed 2427, used 3000" in text


def test_payslip_is_complete(seeded_session):
    view = PayrollCalculationService(seeded_session).calculate("EMP-009", SEP)
    s = view.summary
    assert [d.head_code for d in s.deductions] == ["EE_PF", "TDS", "LWF_EE"]
    assert s.net_pay == D(70000 - 4200 - 2427 - 34)
    assert view.pending_components == []
    assert "NOT YET CALCULATED" not in view.to_text()


def test_not_configured_result_never_deducts():
    from payroll_copilot.calculations.tds import TDSResult

    result = TDSResult.not_configured("no TDS rule for FY 2027-28")
    assert result.applicable and not result.configured and result.deductions() == []
