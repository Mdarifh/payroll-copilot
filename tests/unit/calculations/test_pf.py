from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

import pytest

from payroll_copilot.calculations.pay_summary import summarize_pay
from payroll_copilot.calculations.pf import PFInput, PFResult, calculate_pf
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.core.money import RoundingMode
from payroll_copilot.domain.enums import CeilingProration, RuleStatus

D = Decimal


@dataclass(frozen=True)
class Rule:
    """Stand-in for a PF rule version (the engine only reads attributes)."""

    rule_code: str = "EPF"
    version: int = 2
    effective_from: date = date(2026, 9, 1)
    effective_to: date | None = None
    status: RuleStatus = RuleStatus.VERIFIED
    applicability: list = field(default_factory=list)
    employee_rate: Decimal = D(12)
    employer_rate: Decimal = D(12)
    eps_rate: Decimal = D("8.33")
    edli_rate: Decimal = D("0.5")
    admin_rate: Decimal = D("0.5")
    admin_min_amount: Decimal | None = None
    pf_wage_ceiling: Decimal = D(25000)
    eps_wage_ceiling: Decimal = D(25000)
    edli_wage_ceiling: Decimal = D(25000)
    eps_max_age: int | None = 58
    ceiling_proration: CeilingProration = CeilingProration.NONE
    rounding_mode: RoundingMode = RoundingMode.HALF_UP


NEW = Rule()
OLD = replace(NEW, version=1, effective_from=date(2018, 6, 1), effective_to=date(2026, 8, 31),
              pf_wage_ceiling=D(15000), eps_wage_ceiling=D(15000), edli_wage_ceiling=D(15000))  # fmt: skip


def inp(basic, paid=30, basis=30, **kw) -> PFInput:
    values = dict(employee_code="E1", period="2026-09", pf_wage_lines={"BASIC": D(basic)},
                  paid_days=D(paid), basis_days=D(basis), dob=date(1990, 1, 1))  # fmt: skip
    values.update(kw)
    return PFInput(**values)


def amounts(result: PFResult) -> dict[str, int]:
    return {k: int(v) for k, v in result.amounts.items()}


class TestCeilingAndRates:
    def test_new_rule_caps_at_25000(self):
        r = calculate_pf(inp(30000), NEW)
        assert (r.pf_wage_actual, r.ceiling, r.pf_wage) == (D(30000), D(25000), D(25000))
        assert amounts(r) == {"EE_PF": 3000, "ER_EPF": 917, "EPS": 2083, "EDLI": 125, "EPF_ADMIN": 125}

    def test_old_rule_caps_at_15000(self):
        r = calculate_pf(inp(30000), OLD)
        assert amounts(r) == {"EE_PF": 1800, "ER_EPF": 550, "EPS": 1250, "EDLI": 75, "EPF_ADMIN": 75}

    def test_wage_below_ceiling(self):
        r = calculate_pf(inp(10800), NEW)
        assert amounts(r) == {"EE_PF": 1296, "ER_EPF": 396, "EPS": 900, "EDLI": 54, "EPF_ADMIN": 54}

    def test_lop_reduces_wage_below_ceiling(self):
        assert calculate_pf(inp(18000, paid=27), NEW).amounts["EE_PF"] == D(2160)

    def test_employer_split_always_adds_up(self):
        for basic in (0, 999, 10800, 14999, 15001, 24999, 25000, 25001, 99999):
            r = calculate_pf(inp(basic), NEW)
            assert r.amounts["ER_EPF"] + r.amounts["EPS"] == r.employer_total
            assert r.pf_wage <= NEW.pf_wage_ceiling
            assert r.eps_wage <= NEW.eps_wage_ceiling

    def test_zero_wage(self):
        r = calculate_pf(inp(0), NEW)
        assert set(r.amounts.values()) == {D(0)}


class TestEmployeeOptions:
    def test_not_restricted_contributes_on_actual_but_eps_capped(self):
        r = calculate_pf(inp(35000, wage_restricted=False), NEW)
        assert r.pf_wage == D(35000)
        assert r.amounts["EE_PF"] == D(4200)
        assert r.eps_wage == D(25000) and r.amounts["EPS"] == D(2083)
        assert r.edli_wage == D(25000)
        assert r.amounts["EPF_ADMIN"] == D(175)
        assert "not restricted" in r.trace.to_text()

    def test_not_eps_eligible(self):
        r = calculate_pf(inp(30000, eps_eligible=False), NEW)
        assert r.amounts["EPS"] == 0
        assert r.amounts["ER_EPF"] == D(3000)
        assert "not EPS eligible" in r.components["EPS"].to_text()

    def test_age_58_stops_eps(self):
        r = calculate_pf(inp(30000, dob=date(1968, 8, 1)), NEW)  # 58 on 1 Sep 2026
        assert r.amounts["EPS"] == 0
        assert "age 58 ≥ EPS max age 58" in r.trace.to_text()

    def test_turning_58_within_month_warns(self):
        r = calculate_pf(inp(30000, dob=date(1968, 9, 20)), NEW)
        assert r.amounts["EPS"] == D(2083)
        assert any("reaches age 58 during 2026-09" in w for w in r.warnings)

    def test_no_age_limit_in_rule(self):
        r = calculate_pf(inp(30000, dob=date(1950, 1, 1)), replace(NEW, eps_max_age=None))
        assert r.amounts["EPS"] == D(2083)


class TestRuleOptions:
    def test_ceiling_prorated_by_paid_days(self):
        rule = replace(NEW, ceiling_proration=CeilingProration.PAID_DAYS)
        r = calculate_pf(inp(20000, paid=16), rule)
        assert r.ceiling == D(25000) * 16 / 30
        assert r.amounts["EE_PF"] == D(1600)  # 13333.33 × 12%

    def test_rounding_mode_from_rule(self):
        r = calculate_pf(inp(25000), replace(NEW, rounding_mode=RoundingMode.FLOOR))
        assert r.amounts["EPS"] == D(2082)  # 2082.5 floored
        assert r.amounts["ER_EPF"] == D(918)

    def test_admin_minimum_is_establishment_level(self):
        r = calculate_pf(inp(30000), replace(NEW, admin_min_amount=D(500)))
        assert r.amounts["EPF_ADMIN"] == D(125)
        assert any("per establishment" in w for w in r.warnings)

    def test_arrear_wage_included_with_warning(self):
        lines = {"BASIC": D(13920), "BASIC (arrear)": D(465)}
        r = calculate_pf(inp(0, pf_wage_lines=lines), NEW)
        assert r.pf_wage == D(14385) and r.amounts["EE_PF"] == D(1726)
        assert any("Arrear PF wage" in w for w in r.warnings)

    def test_rule_warnings_carried_into_trace(self):
        r = calculate_pf(inp(30000), NEW, ["EPF v2 is UNVERIFIED"])
        assert "WARNING: EPF v2 is UNVERIFIED" in r.components["EE_PF"].to_text()

    def test_invalid_basis(self):
        with pytest.raises(CalculationError):
            calculate_pf(inp(1000, basis=0), NEW)


class TestOutputs:
    def test_component_traces(self):
        r = calculate_pf(inp(30000), NEW)
        ee = r.components["EE_PF"]
        assert ee.value == D(3000) and ee.rule.label == "EPF v2 (2026-09-01 to open)"
        assert [s.label for s in ee.steps] == ["PF wage earned", "PF wage ceiling", "PF wage", "Employee PF"]
        assert r.components["ER_EPF"].to_text().endswith("Result: ₹917")

    def test_buckets_are_correct(self, heads):
        r = calculate_pf(inp(30000), NEW)
        assert [line.head_code for line in r.deductions()] == ["EE_PF"]
        assert [line.head_code for line in r.employer_contributions()] == ["ER_EPF", "EPS", "EDLI", "EPF_ADMIN"]
        summary = summarize_pay([], r.deductions(), r.employer_contributions(), heads)
        assert summary.total_deductions == D(3000)
        assert summary.total_employer_contributions == D(3250)

    def test_not_applicable(self):
        r = PFResult.not_applicable("excluded")
        assert r.deductions() == [] and r.employer_contributions() == []
        assert set(r.amounts.values()) == {D(0)}
