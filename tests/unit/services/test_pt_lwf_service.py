from decimal import Decimal

import pytest

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.services.lwf_service import LWFService, render_lwf
from payroll_copilot.services.payroll_service import PayrollCalculationService
from payroll_copilot.services.pt_service import PTService, half_year_periods, render_pt

SEP = PayrollPeriod(2026, 9)

EXPECTED_PT_SEP = {
    "ARIF-112": 200,   # MH male > 10000
    "EMP-002": 200,    # KA ≥ 25000 (earned 36000)
    "EMP-003": 130,    # WB 15001-25000
    "EMP-004": 1250,   # TN half-yearly (Apr-Sep income) deducted in Sep
    "EMP-005": 0,      # DL not levied
    "EMP-006": 200,    # GJ ≥ 12000
    "EMP-007": 200,
    "EMP-008": 150,    # TG 15001-20000
    "EMP-009": 0,      # HR not levied
    "EMP-010": 0,      # MH female ≤ 25000
}  # fmt: skip


@pytest.mark.parametrize(("code", "amount"), EXPECTED_PT_SEP.items())
def test_pt_september(seeded_session, code, amount):
    assert PTService(seeded_session).calculate(code, SEP).amount == Decimal(amount)


def test_tn_half_year_income_breakdown(seeded_session):
    r = PTService(seeded_session).calculate("EMP-004", SEP)
    keys = list(r.trace.inputs["slab_wage_breakdown"])
    assert keys[0] == "2026-04 (estimated from structure)"
    assert "2026-08 (calculated)" in keys and keys[-1] == "2026-09 (this month)"
    assert r.slab_wage == Decimal(310000)
    assert any("structure-based estimates" in w for w in r.warnings)


def test_tn_not_deducted_in_august(seeded_session):
    r = PTService(seeded_session).calculate("EMP-004", PayrollPeriod(2026, 8))
    assert r.amount == 0 and "deducts in months" in r.reason


def test_half_year_periods():
    assert [str(p) for p in half_year_periods(SEP)][::5] == ["2026-04", "2026-09"]
    assert str(half_year_periods(PayrollPeriod(2027, 1))[0]) == "2026-10"


def test_unconfigured_state_is_flagged_not_zeroed(seeded_session):
    employee = EmployeeRepository(seeded_session).get_by_code("ARIF-112")
    employee.work_state = "GA"
    seeded_session.flush()
    r = PTService(seeded_session).calculate("ARIF-112", SEP)
    assert not r.configured and r.deductions() == []
    assert "NOT CALCULATED" in render_pt(r, "ARIF-112", "2026-09")
    view = PayrollCalculationService(seeded_session).calculate("ARIF-112", SEP)
    assert any("PT NOT CALCULATED for state GA" in w for w in view.warnings)
    assert any("LWF NOT CALCULATED for state GA" in w for w in view.warnings)


def test_pt_not_applicable_flag(seeded_session):
    employee = EmployeeRepository(seeded_session).get_by_code("ARIF-112")
    employee.pt_applicable = False
    seeded_session.flush()
    assert not PTService(seeded_session).calculate("ARIF-112", SEP).applicable


def test_lwf(seeded_session):
    service = LWFService(seeded_session)
    rahul = service.calculate("EMP-009", SEP)
    assert (rahul.employee_amount, rahul.employer_amount) == (Decimal(34), Decimal(68))
    arif = service.calculate("ARIF-112", SEP)
    assert arif.deductions() == [] and "₹0" in render_lwf(arif, "ARIF-112", "2026-09")


def test_payslip_now_has_pt_and_lwf(seeded_session):
    view = PayrollCalculationService(seeded_session).calculate("EMP-009", SEP)
    s = view.summary
    lines = {d.head_code: d.amount for d in s.deductions}
    assert lines["LWF_EE"] == Decimal(34) and "PT" not in lines  # HR: LWF yes, PT not levied
    assert "LWF_ER" in [c.head_code for c in s.employer_contributions]
    assert "PT: ₹0 - PT not levied in Haryana" in view.to_text()
