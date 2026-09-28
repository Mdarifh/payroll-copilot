"""TDS engine, using the real (seeded) TDS rules."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from payroll_copilot.calculations.tds import (
    Declaration,
    TDSInput,
    calculate_tds,
    compute_annual_tax,
)
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.db.models import TDSRule

D = Decimal


@pytest.fixture
def tds_rule(seeded_session):
    def _get(code):
        return seeded_session.scalar(select(TDSRule).where(TDSRule.rule_code == code))

    return _get


class TestAnnualTaxNewRegime:
    @pytest.mark.parametrize(
        ("income", "tax"),
        [
            (400000, 0),
            (645000, 0),        # 12250 fully rebated (≤ 12L)
            (1200000, 0),       # 60000 rebated exactly at the limit
            (1210000, 10400),   # marginal relief: tax 61500 capped at 10000, + 4% cess
            (1500000, 109200),  # 20000 + 40000 + 45000 = 105000 + cess 4200
        ],
    )  # fmt: skip
    def test_slabs_rebate_marginal_relief(self, tds_rule, income, tax):
        assert compute_annual_tax(income, tds_rule("TDS-NEW")).total_tax == D(tax)

    def test_marginal_relief_note(self, tds_rule):
        result = compute_annual_tax(1210000, tds_rule("TDS-NEW"))
        assert result.rebate == D(51500)
        assert "marginal relief on rebate" in result.notes[0]

    def test_surcharge(self, tds_rule):
        r = compute_annual_tax(6000000, tds_rule("TDS-NEW"))
        assert r.tax_after_rebate == D(1380000)
        assert (r.surcharge_rate, r.surcharge) == (D(10), D(138000))
        assert r.total_tax == D("1578720")  # (1380000 + 138000) × 1.04

    def test_surcharge_marginal_relief(self, tds_rule):
        r = compute_annual_tax(5010000, tds_rule("TDS-NEW"))
        assert r.tax_after_rebate == D(1083000)
        assert r.surcharge == D(7000)  # capped: 1080000 + 10000 − 1083000
        assert any("marginal relief on surcharge" in n for n in r.notes)

    def test_negative_income_is_zero(self, tds_rule):
        assert compute_annual_tax(-5, tds_rule("TDS-NEW")).total_tax == 0


class TestAnnualTaxOldRegime:
    @pytest.mark.parametrize(
        ("code", "income", "tax"),
        [
            ("TDS-OLD", 500000, 0),                 # rebate up to 12500
            ("TDS-OLD", 510000, 15080),             # 14500 + cess, no marginal relief in old regime
            ("TDS-OLD-SENIOR", 590000, 29120),      # 10000 + 18000 + cess
            ("TDS-OLD-SUPER-SENIOR", 590000, 18720),  # 5L exempt; 90000 × 20% + cess
        ],
    )  # fmt: skip
    def test_old_regime(self, tds_rule, code, income, tax):
        assert compute_annual_tax(income, tds_rule(code)).total_tax == D(tax)


def inp(monthly: int, months=12, **kw) -> TDSInput:
    values = dict(
        employee_code="E1", period="2026-09",
        monthly_taxable={f"m{i}": D(monthly) for i in range(months)},
        remaining_months=7,
    )  # fmt: skip
    values.update(kw)
    return TDSInput(**values)


class TestMonthlyTDS:
    def test_old_regime_with_declarations_and_history(self, tds_rule):
        decl = [Declaration(section_code="80C", amount=D(200000)),
                Declaration(section_code="80D", amount=D(50000))]  # fmt: skip
        r = calculate_tds(inp(70000, declarations=decl, tds_already_deducted=D(12133)),
                          tds_rule("TDS-OLD-SENIOR"))  # fmt: skip
        assert r.section_deductions == {"80C": D(150000), "80D": D(50000)}  # 80C capped
        assert r.annual.taxable_income == D(590000)
        assert r.monthly_tds == D(2427)  # (29120 − 12133) / 7

    def test_new_regime_ignores_declarations_and_pt(self, tds_rule):
        r = calculate_tds(
            inp(150000, declarations=[Declaration(section_code="80C", amount=D(150000))],
                professional_tax_annual=D(2500)),
            tds_rule("TDS-NEW"),
        )  # fmt: skip
        assert r.section_deductions == {} and r.professional_tax == 0
        assert r.annual.taxable_income == D(1800000 - 75000)
        assert any("80C ₹150000 ignored" in w for w in r.warnings)
        assert any("PT not deductible" in w for w in r.warnings)

    def test_pt_deducted_in_old_regime(self, tds_rule):
        r = calculate_tds(inp(70000, professional_tax_annual=D(2500)), tds_rule("TDS-OLD"))
        assert r.professional_tax == D(2500)

    def test_already_deducted_more_than_tax(self, tds_rule):
        r = calculate_tds(inp(30000, tds_already_deducted=D(5000)), tds_rule("TDS-NEW"))
        assert r.monthly_tds == 0
        assert any("exceeds projected annual tax" in w for w in r.warnings)

    def test_final_month_recovers_balance(self, tds_rule):
        r = calculate_tds(inp(150000, remaining_months=1, tds_already_deducted=D(100000)),
                          tds_rule("TDS-NEW"))  # fmt: skip
        assert r.monthly_tds == r.annual.total_tax - D(100000)

    def test_override(self, tds_rule):
        r = calculate_tds(inp(150000, override_amount=D(9999), override_reason="test"),
                          tds_rule("TDS-NEW"))  # fmt: skip
        assert r.overridden and r.monthly_tds == D(9999)
        assert r.computed_monthly_tds != D(9999)
        assert "Manual override" in r.trace.to_text()
        assert r.deductions()[0].amount == D(9999)

    def test_remaining_months_must_be_positive(self, tds_rule):
        with pytest.raises(CalculationError):
            calculate_tds(inp(1000, remaining_months=0), tds_rule("TDS-NEW"))

    def test_trace(self, tds_rule):
        text = calculate_tds(inp(150000), tds_rule("TDS-NEW")).trace.to_text()
        assert "Standard deduction: min(75000, gross) = ₹75,000" in text
        assert "Slab 1200000-1600000 @ 15%" in text
