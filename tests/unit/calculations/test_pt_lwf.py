"""PT and LWF engines, using the real (seeded) state rules."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from payroll_copilot.calculations.lwf import LWFInput, calculate_lwf
from payroll_copilot.calculations.pt import PTInput, calculate_pt, select_slab
from payroll_copilot.core.money import RoundingMode
from payroll_copilot.db.models import LWFRule, PTRule
from payroll_copilot.domain.enums import Frequency, Gender, RuleStatus

D = Decimal
M, F, OTH = Gender.MALE, Gender.FEMALE, Gender.OTHER


@pytest.fixture
def pt_rule(seeded_session):
    def _get(state):
        return seeded_session.scalar(select(PTRule).where(PTRule.state_code == state))

    return _get


@pytest.fixture
def lwf_rule(seeded_session):
    def _get(state):
        return seeded_session.scalar(select(LWFRule).where(LWFRule.state_code == state))

    return _get


def pt(rule, wage, gender=M, period="2026-09", **kw):
    return calculate_pt(
        PTInput(employee_code="E1", period=period, gender=gender, slab_wage=D(str(wage)), **kw),
        rule,
    )


class TestMonthlyStates:
    @pytest.mark.parametrize(
        ("wage", "gender", "period", "amount"),
        [
            (7500, M, "2026-09", 0), ("7500.01", M, "2026-09", 175), (10000, M, "2026-09", 175),
            (60000, M, "2026-09", 200), (60000, M, "2027-02", 300),
            (25000, F, "2026-09", 0), ("25000.01", F, "2026-09", 200), (60000, F, "2027-02", 300),
            (9000, OTH, "2026-09", 175),  # no 'O' slab set -> default set
        ],
    )  # fmt: skip
    def test_maharashtra(self, pt_rule, wage, gender, period, amount):
        assert pt(pt_rule("MH"), wage, gender, period).amount == D(amount)

    @pytest.mark.parametrize(("wage", "amount"), [("24999.99", 0), (25000, 200)])
    def test_karnataka(self, pt_rule, wage, amount):
        assert pt(pt_rule("KA"), wage).amount == D(amount)

    @pytest.mark.parametrize(
        ("wage", "amount"), [(10000, 0), (12000, 110), (18000, 130), (30000, 150), (40001, 200)]
    )
    def test_west_bengal(self, pt_rule, wage, amount):
        assert pt(pt_rule("WB"), wage).amount == D(amount)

    def test_meghalaya_keeps_paise(self, pt_rule):
        assert pt(pt_rule("ML"), 20000).amount == D("83.33")

    def test_not_levied(self, pt_rule):
        r = pt(pt_rule("DL"), 90000)
        assert r.amount == 0 and not r.levied and r.deductions() == []
        assert "not levied in Delhi" in r.reason


class TestPeriodicStates:
    def test_tamil_nadu_deducts_in_september_on_half_year_income(self, pt_rule):
        r = pt(pt_rule("TN"), 310000, slab_wage_breakdown={"2026-04 (estimated)": D(50000)})
        assert r.amount == D(1250) and r.deductions()[0].amount == D(1250)
        assert "slabs on half-yearly income" in r.trace.to_text()

    def test_tamil_nadu_no_deduction_in_august(self, pt_rule):
        r = pt(pt_rule("TN"), 310000, period="2026-08")
        assert r.amount == 0 and not r.deduction_month and r.deductions() == []
        assert "deducts in months [3, 9]" in r.reason

    def test_tamil_nadu_low_income(self, pt_rule):
        assert pt(pt_rule("TN"), 21000).amount == 0

    def test_leaver_before_deduction_month_warns(self, pt_rule):
        r = pt(pt_rule("TN"), 100000, period="2026-08", leaves_this_month=True)
        assert any("recover PT" in w for w in r.warnings)

    @pytest.mark.parametrize(("period", "amount"), [("2026-09", 166), ("2027-03", 174)])
    def test_madhya_pradesh_annual_income_with_march_override(self, pt_rule, period, amount):
        assert pt(pt_rule("MP"), 350000, period=period).amount == D(amount)

    def test_bihar_yearly(self, pt_rule):
        assert pt(pt_rule("BR"), 600000, period="2027-03").amount == D(2000)
        assert pt(pt_rule("BR"), 600000, period="2026-09").amount == 0


def test_select_slab_band_text(pt_rule):
    slab = select_slab(pt_rule("MH"), F, D(30000))
    assert slab.band == "25000 < wage ≤ ∞" and slab.month_overrides == {"2": D(300)}


@dataclass(frozen=True)
class FakeLWF:
    rule_code: str = "LWF-XX"
    version: int = 1
    effective_from: date = date(2025, 4, 1)
    effective_to: date | None = None
    status: RuleStatus = RuleStatus.VERIFIED
    applicability: list = field(default_factory=list)
    state_code: str = "XX"
    state_name: str = "Test"
    frequency: Frequency = Frequency.MONTHLY
    deduction_months: tuple = tuple(range(1, 13))
    employee_amount: Decimal | None = D(10)
    employer_amount: Decimal | None = D(20)
    employee_rate: Decimal | None = None
    employee_max_amount: Decimal | None = None
    employer_multiplier: Decimal | None = None
    wage_upper_limit: Decimal | None = D(15000)
    rounding_mode: RoundingMode = RoundingMode.HALF_UP


def lwf(rule, wage, period="2026-09"):
    return calculate_lwf(LWFInput(employee_code="E1", period=period, lwf_wage=D(wage)), rule)


class TestLWF:
    @pytest.mark.parametrize(("wage", "ee", "er"), [(70000, 34, 68), (10000, 20, 40)])
    def test_haryana_rate_with_cap(self, lwf_rule, wage, ee, er):
        r = lwf(lwf_rule("HR"), wage)
        assert (r.employee_amount, r.employer_amount) == (D(ee), D(er))
        assert [line.head_code for line in r.deductions()] == ["LWF_EE"]
        assert [line.head_code for line in r.employer_contributions()] == ["LWF_ER"]

    def test_maharashtra_half_yearly(self, lwf_rule):
        assert (lwf(lwf_rule("MH"), 60000, "2026-12").employee_amount) == D(25)
        september = lwf(lwf_rule("MH"), 60000)
        assert september.deductions() == [] and "deducts in months [6, 12]" in september.reason

    def test_wage_upper_limit(self):
        assert lwf(FakeLWF(), 14000).employee_amount == D(10)
        above = lwf(FakeLWF(), 16000)
        assert above.deductions() == [] and "above limit" in above.reason
