from decimal import Decimal

import pytest

from payroll_copilot.calculations.pay_summary import compute_wage_base, summarize_pay
from payroll_copilot.core.exceptions import StructureError
from payroll_copilot.domain.salary import PayLine

D = Decimal


def lines(**amounts) -> list[PayLine]:
    return [PayLine(head_code=k, amount=D(v)) for k, v in amounts.items()]


EARNINGS = lines(BASIC=30000, HRA=15000, SPECIAL=12150, CONVEYANCE=1600, MEDICAL=1250)
DEDUCTIONS = lines(EE_PF=3000, PT=200, TDS=2500)
EMPLOYER = lines(ER_EPF=917, EPS=2083, EDLI=125, EPF_ADMIN=125)


def test_buckets_and_totals(heads):
    s = summarize_pay(EARNINGS, DEDUCTIONS, EMPLOYER, heads)
    assert s.gross_earnings == D(60000)
    assert s.total_deductions == D(5700)
    assert s.net_pay == D(54300)  # employer contributions NOT subtracted
    assert s.total_employer_contributions == D(3250)
    assert s.monthly_ctc == D(63250)  # gross + employer contributions
    assert [b.head_code for b in s.deductions] == ["EE_PF", "PT", "TDS"]
    assert [b.head_code for b in s.employer_contributions] == ["ER_EPF", "EPS", "EDLI", "EPF_ADMIN"]
    assert s.warnings == []


def test_wage_bases_follow_head_flags(heads):
    s = summarize_pay(EARNINGS, [], [], heads)
    assert s.wage_bases["PF_WAGE"] == D(30000)  # only Basic is PF wage
    assert s.wage_bases["ESIC_WAGE"] == D(60000)
    assert s.wage_bases["TAXABLE_EARNINGS"] == D(60000)


def test_bonus_excluded_from_esic_and_pf_wage(heads):
    earnings = EARNINGS + lines(BONUS=5000)
    s = summarize_pay(earnings, [], [], heads)
    assert s.gross_earnings == D(65000)
    assert s.wage_bases["ESIC_WAGE"] == D(60000)
    assert s.wage_bases["PT_WAGE"] == D(65000)


def test_wage_base_trace(heads):
    earnings = EARNINGS + [PayLine(head_code="BASIC", amount=D(2500), is_arrear=True)]
    result = compute_wage_base("PF_WAGE", earnings, heads, employee_code="E1", period="2026-09")
    assert result.value == D(32500)
    assert result.breakdown == {"BASIC": D(30000), "BASIC (arrear)": D(2500)}
    assert "HRA" in result.steps[1].values["heads"]


def test_unknown_wage_base(heads):
    with pytest.raises(StructureError, match="Unknown wage base"):
        compute_wage_base("GRATUITY_WAGE", EARNINGS, heads)


@pytest.mark.parametrize(
    ("earnings", "deductions", "employer", "message"),
    [
        (EARNINGS, lines(ER_EPF=100), [], "is a EMPLOYER_CONTRIBUTION, not a DEDUCTION"),
        (EARNINGS, [], lines(EE_PF=100), "is a DEDUCTION, not a EMPLOYER_CONTRIBUTION"),
        (lines(EE_PF=100), [], [], "is a DEDUCTION, not a EARNING"),
        (lines(XYZ=1), [], [], "Unknown salary head"),
        (lines(BASIC=-5), [], [], "negative amount"),
        (EARNINGS, lines(PT=-200), [], "negative amount"),
    ],
)
def test_buckets_never_mix(heads, earnings, deductions, employer, message):
    with pytest.raises(StructureError, match=message):
        summarize_pay(earnings, deductions, employer, heads)


def test_negative_arrear_allowed_as_recovery(heads):
    earnings = EARNINGS + [PayLine(head_code="BASIC", amount=D(-1000), is_arrear=True)]
    assert summarize_pay(earnings, [], [], heads).gross_earnings == D(59000)


def test_negative_net_warns(heads):
    s = summarize_pay(lines(BASIC=1000), lines(LOAN=1500), [], heads)
    assert s.net_pay == D(-500)
    assert "Net pay is negative" in s.warnings[0]
