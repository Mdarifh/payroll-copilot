from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

import pytest

from payroll_copilot.calculations.esic import (
    ESICInput,
    ESICResult,
    calculate_esic,
    contribution_period_for,
)
from payroll_copilot.calculations.pay_summary import summarize_pay
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.core.money import RoundingMode
from payroll_copilot.domain.enums import RuleStatus

D = Decimal


@dataclass(frozen=True)
class Rule:
    rule_code: str = "ESIC"
    version: int = 1
    effective_from: date = date(2019, 7, 1)
    effective_to: date | None = None
    status: RuleStatus = RuleStatus.VERIFIED
    applicability: list = field(default_factory=list)
    employee_rate: Decimal = D("0.75")
    employer_rate: Decimal = D("3.25")
    wage_threshold: Decimal = D(21000)
    disabled_wage_threshold: Decimal | None = D(25000)
    contribution_period_start_months: tuple = (4, 10)
    rounding_mode: RoundingMode = RoundingMode.CEIL


RULE = Rule()


def inp(earned, coverage_wage=None, coverage_date=date(2026, 4, 1), period="2026-09", **kw):
    lines = earned if isinstance(earned, dict) else {"BASIC": D(earned)}
    return ESICInput(
        employee_code="E1", period=period, esic_wage_lines=lines,
        coverage_wage=D(coverage_wage if coverage_wage is not None else sum(lines.values())),
        coverage_date=coverage_date, **kw,
    )  # fmt: skip


class TestContributionPeriod:
    @pytest.mark.parametrize(
        ("period", "start", "end"),
        [
            ("2026-04", date(2026, 4, 1), date(2026, 9, 30)),
            ("2026-09", date(2026, 4, 1), date(2026, 9, 30)),
            ("2026-10", date(2026, 10, 1), date(2027, 3, 31)),
            ("2027-02", date(2026, 10, 1), date(2027, 3, 31)),
            ("2027-03", date(2026, 10, 1), date(2027, 3, 31)),
        ],
    )
    def test_half_years(self, period, start, end):
        assert contribution_period_for(PayrollPeriod.parse(period), [4, 10]) == (start, end)

    def test_other_configuration(self):
        assert contribution_period_for(PayrollPeriod(2026, 2), [1]) == (date(2026, 1, 1), date(2026, 12, 31))

    def test_empty_configuration(self):
        with pytest.raises(CalculationError):
            contribution_period_for(PayrollPeriod(2026, 2), [])


class TestContributions:
    def test_rates_and_ceil_rounding(self):
        r = calculate_esic(inp(18000), RULE)
        assert r.covered
        assert r.amounts == {"EE_ESIC": D(135), "ER_ESIC": D(585)}

    def test_fraction_rounded_up(self):
        r = calculate_esic(inp(19133, coverage_wage=20500), RULE)
        assert r.amounts == {"EE_ESIC": D(144), "ER_ESIC": D(622)}  # 143.50 / 621.82 ceiled
        assert "rounded CEIL" in r.components["EE_ESIC"].to_text()

    def test_exactly_at_threshold_is_covered(self):
        assert calculate_esic(inp(21000), RULE).covered

    def test_just_above_threshold_not_covered(self):
        r = calculate_esic(inp(D("21000.01")), RULE)
        assert not r.covered and r.deductions() == [] and r.employer_contributions() == []
        assert "exceeds wage threshold 21000" in r.reason

    def test_disabled_threshold(self):
        r = calculate_esic(inp(24000, is_disabled=True), RULE)
        assert r.covered and r.threshold == D(25000)

    def test_rounding_from_rule(self):
        r = calculate_esic(inp(19133, coverage_wage=20500), replace(RULE, rounding_mode=RoundingMode.HALF_UP))
        assert r.amounts["EE_ESIC"] == D(143)


class TestCoverage:
    def test_stays_covered_after_increment_within_period(self):
        r = calculate_esic(inp(23000, coverage_wage=20000), RULE)
        assert r.covered
        assert r.amounts["EE_ESIC"] == D(173)  # 172.5 ceiled, on actual earned wage
        assert any("stays covered until 2026-09-30" in w for w in r.warnings)

    def test_new_period_re_evaluates(self):
        r = calculate_esic(inp(23000, coverage_date=date(2026, 10, 1), period="2026-10"), RULE)
        assert not r.covered

    def test_arrear_counts_for_contribution_not_coverage(self):
        lines = {"BASIC": D(20000), "BASIC (arrear)": D(3000)}
        r = calculate_esic(inp(lines, coverage_wage=20000), RULE)
        assert r.covered and r.esic_wage == D(23000)
        assert any("not used for coverage" in w for w in r.warnings)

    def test_mid_period_joiner_tested_on_doj(self):
        r = calculate_esic(inp(9333, coverage_wage=17500, coverage_date=date(2026, 9, 15)), RULE)
        assert r.covered and r.coverage_date == date(2026, 9, 15)

    def test_coverage_date_must_be_in_period(self):
        with pytest.raises(CalculationError, match="outside contribution period"):
            calculate_esic(inp(1000, coverage_date=date(2026, 3, 31)), RULE)

    def test_zero_wage(self):
        r = calculate_esic(inp(0, coverage_wage=18000), RULE)
        assert r.amounts == {"EE_ESIC": D(0), "ER_ESIC": D(0)}
        assert any("No ESIC wage" in w for w in r.warnings)


class TestOutputs:
    def test_trace(self):
        text = calculate_esic(inp(18000), RULE).trace.to_text()
        assert "Contribution period: 2026-04-01 to 2026-09-30" in text
        assert "4. Employee ESIC: 18000 × 0.75% = ₹135" in text

    def test_buckets(self, heads):
        r = calculate_esic(inp(18000), RULE)
        s = summarize_pay([], r.deductions(), r.employer_contributions(), heads)
        assert (s.total_deductions, s.total_employer_contributions) == (D(135), D(585))

    def test_not_applicable(self):
        r = ESICResult.not_applicable("x")
        assert not r.applicable and r.deductions() == []
