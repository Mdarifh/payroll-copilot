from decimal import Decimal

import pytest

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.domain.salary import PayLine
from payroll_copilot.services.salary_service import SalaryService

SEP = PayrollPeriod(2026, 9)

EXPECTED_SEP_GROSS = {
    "ARIF-112": 60000,  # full month
    "EMP-002": 36000,   # 3 LOP: 40000 × 27/30
    "EMP-003": 18000,
    "EMP-004": 60000,   # new structure 55000 + Aug increment arrear 5000
    "EMP-005": 18667,   # joiner 16/30, rounded per head
    "EMP-006": 30000,   # leaver 20/30
    "EMP-007": 80000,
    "EMP-008": 19133,   # 2 LOP
    "EMP-009": 70000,
    "EMP-010": 23975,   # 29/30 = 23200 + LOP reversal at Aug rate 775
}  # fmt: skip


@pytest.mark.parametrize(("code", "gross"), EXPECTED_SEP_GROSS.items())
def test_seed_gross_september(seeded_session, code, gross):
    view = SalaryService(seeded_session).calculate(code, SEP)
    assert view.salary.gross_earnings == Decimal(gross)
    assert view.summary.gross_earnings == Decimal(gross)
    assert view.summary.total_deductions == 0  # statutory engines not wired yet


def test_lop_reversal_uses_august(seeded_session):
    view = SalaryService(seeded_session).calculate("EMP-010", SEP)
    arrears = {line.head_code: line for line in view.salary.lines if line.is_arrear}
    assert arrears["BASIC"].amount == Decimal(465)  # 14400 × 1 / 31
    assert arrears["BASIC"].basis_days == Decimal(31)
    assert any("assumed to relate to 2026-08" in w for w in view.warnings)


def test_reduction_explains_lop(seeded_session):
    r = SalaryService(seeded_session).calculate("EMP-002", SEP).salary.reduction
    assert (r["full_month_gross"], r["reduction_lop"], r["earned_gross"]) == (
        Decimal(40000), Decimal(4000), Decimal(36000),
    )  # fmt: skip


def test_variable_earnings_passed_through(seeded_session):
    view = SalaryService(seeded_session).calculate(
        "ARIF-112", SEP, variable_earnings=[PayLine(head_code="BONUS", amount=Decimal(2500))]
    )
    assert view.salary.gross_earnings == Decimal(62500)
    assert view.summary.wage_bases["PF_WAGE"] == Decimal(30000)


def test_missing_attendance(seeded_session):
    with pytest.raises(NotFoundError, match="No attendance"):
        SalaryService(seeded_session).calculate("EMP-002", PayrollPeriod(2026, 8))


def test_text(seeded_session):
    text = SalaryService(seeded_session).calculate("EMP-002", SEP).to_text()
    assert "Paid days 27 of 30 (CALENDAR basis)" in text
    assert "Less: LOP" in text and "-₹4,000" in text
    assert "Net before statutory deductions: ₹36,000" in text
