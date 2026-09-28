from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from payroll_copilot.calculations.arrears import (
    ArrearChange,
    ArrearMonthInput,
    ArrearMonthResult,
    arrear_lines_for_month,
    pf_arrear_impact,
    summarize_arrears,
)
from payroll_copilot.calculations.fnf import FnFInput, calculate_fnf, service_length
from payroll_copilot.core.exceptions import CalculationError
from payroll_copilot.domain.enums import ComparisonStatus
from payroll_copilot.domain.salary import PayLine
from tests.unit.calculations.test_pf import NEW, OLD

D = Decimal
PF_EMPLOYEE = dict(employee_code="E1", wage_restricted=True, eps_eligible=True, dob=date(1990, 1, 1))


def month(period="2026-08", paid=31, basis=31, changes=None, components=None) -> ArrearMonthInput:
    changes = changes or {"BASIC": (25000, 27500), "HRA": (12500, 13750)}
    components = components or {"BASIC": 27500, "HRA": 13750, "SPECIAL": 10900}
    return ArrearMonthInput(
        period=period, paid_days=D(paid), basis_days=D(basis), days_source="attendance",
        changes=[ArrearChange(head_code=h, old_monthly_amount=D(o), new_monthly_amount=D(n))
                 for h, (o, n) in changes.items()],
        new_components=[PayLine(head_code=h, amount=D(a)) for h, a in components.items()],
    )  # fmt: skip


class TestArrearLines:
    def test_full_month(self, heads):
        lines = arrear_lines_for_month(month(), heads)
        assert {line.head_code: line.amount for line in lines} == {"BASIC": D(2500), "HRA": D(1250)}
        assert all(line.kind == "ARREAR" and line.is_arrear for line in lines)
        assert lines[0].source == "Arrear 2026-08"

    def test_prorated_by_paid_days_of_arrear_month(self, heads):
        lines = arrear_lines_for_month(month(paid=27, basis=30), heads)
        assert lines[0].amount == D(2250)  # 2500 × 27/30
        assert lines[0].formula == "(27500 − 25000) × 27 / 30"

    def test_non_prorated_head_gets_full_difference(self, heads):
        heads = {**heads, "HRA": heads["HRA"].model_copy(update={"is_prorated": False})}
        lines = arrear_lines_for_month(month(paid=15, basis=30), heads)
        assert {line.head_code: line.amount for line in lines}["HRA"] == D(1250)

    def test_negative_arrear_is_a_recovery(self, heads):
        lines = arrear_lines_for_month(month(changes={"BASIC": (27500, 25000)}), heads)
        assert lines[0].amount == D(-2500)

    @pytest.mark.parametrize(("kw", "message"), [
        ({"changes": {"EE_PF": (0, 100)}}, "not an earning"),
        ({"basis": 0}, "basis days"),
    ])  # fmt: skip
    def test_invalid(self, heads, kw, message):
        with pytest.raises(CalculationError, match=message):
            arrear_lines_for_month(month(**kw), heads)


class TestPFOnArrear:
    def test_no_change_when_old_salary_already_above_old_ceiling(self, heads):
        old, new, impact = pf_arrear_impact(month(), heads, OLD, PF_EMPLOYEE)
        assert old["EE_PF"] == new["EE_PF"] == D(1800)
        assert set(impact.values()) == {D(0)}

    def test_below_ceiling_increase(self, heads):
        m = month(changes={"BASIC": (10000, 14000)}, components={"BASIC": 14000, "HRA": 5600})
        _, _, impact = pf_arrear_impact(m, heads, OLD, PF_EMPLOYEE)
        assert impact["EE_PF"] == D(480)
        assert impact["EPS"] + impact["ER_EPF"] == D(480)

    def test_same_arrear_different_rule_different_pf(self, heads):
        m = month(period="2026-09", paid=30, basis=30, changes={"BASIC": (20000, 30000)},
                  components={"BASIC": 30000})  # fmt: skip
        assert pf_arrear_impact(m, heads, OLD, PF_EMPLOYEE)[2]["EE_PF"] == 0  # capped at 15k
        assert pf_arrear_impact(m, heads, NEW, PF_EMPLOYEE)[2]["EE_PF"] == D(600)  # 2400→3000

    def test_actual_wage_employee(self, heads):
        m = month(changes={"BASIC": (30000, 35000)}, components={"BASIC": 35000})
        emp = {**PF_EMPLOYEE, "wage_restricted": False}
        assert pf_arrear_impact(m, heads, replace(OLD), emp)[2]["EE_PF"] == D(600)


class TestSummary:
    def result(self, heads, uploaded):
        lines = arrear_lines_for_month(month(), heads)
        m = ArrearMonthResult(period="2026-08", paid_days=D(31), basis_days=D(31), lines=lines,
                              pf_rule="EPF v1", pf_impact={"EE_PF": D(120), "ER_EPF": D(20),
                                                           "EPS": D(100), "EDLI": D(5),
                                                           "EPF_ADMIN": D(5)})  # fmt: skip
        return summarize_arrears("E1", "2026-09", [m], uploaded, [])

    def test_comparisons(self, heads):
        r = self.result(heads, {"BASIC": D(2500), "HRA": D(1000)})
        status = {c.head_code: (c.status, c.difference) for c in r.comparisons}
        assert status["BASIC"] == (ComparisonStatus.MATCH, D(0))
        assert status["HRA"] == (ComparisonStatus.MISMATCH, D(-250))
        assert any("Uploaded arrear for HRA is 1000, expected 1250" in w for w in r.warnings)

    def test_missing_upload_and_pf_lines(self, heads):
        r = self.result(heads, {})
        assert {c.status for c in r.comparisons} == {ComparisonStatus.MISSING_ACTUAL}
        assert r.total == D(3750)
        assert r.deductions() == [PayLine(head_code="EE_PF", amount=D(120), is_arrear=True)]
        assert [c.head_code for c in r.employer_contributions()] == ["ER_EPF", "EPS", "EDLI", "EPF_ADMIN"]
        assert "PF on arrear 2026-08 (EPF v1)" in r.trace.to_text()


def fnf_input(**kw) -> FnFInput:
    values = dict(
        employee_code="E1", settlement_period="2026-09", doj=date(2020, 1, 6),
        lwd=date(2026, 9, 20),
        full_month_components=[PayLine(head_code=h, amount=D(a)) for h, a in
                               {"BASIC": 22500, "HRA": 11250, "SPECIAL": 8400,
                                "CONVEYANCE": 1600, "MEDICAL": 1250}.items()],
        leave_encashment_days=D(8), notice_period_days=60, notice_days_served=60,
        encashment_heads=["BASIC"], encashment_divisor=D(30),
        notice_heads=["GROSS"], notice_divisor=D(30),
    )  # fmt: skip
    values.update(kw)
    return FnFInput(**values)


class TestFnF:
    def test_encashment_basic_over_30(self, heads):
        r = calculate_fnf(fnf_input(), heads)
        assert r.leave_encashment == D(6000) and r.notice_recovery == 0
        assert r.earnings() == [PayLine(head_code="LEAVE_ENCASH", amount=D(6000))]
        assert r.deductions() == []

    def test_notice_shortfall_on_gross(self, heads):
        r = calculate_fnf(fnf_input(notice_days_served=45), heads)
        assert r.notice_shortfall_days == 15
        assert r.notice_recovery == D(22500)  # 45000 / 30 × 15
        assert r.deductions()[0].head_code == "NOTICE_RECOVERY"

    def test_encashment_on_gross_and_other_recovery(self, heads):
        r = calculate_fnf(fnf_input(encashment_heads=["GROSS"], other_recovery=D(1000)), heads)
        assert r.leave_encashment == D(12000)
        assert [line.head_code for line in r.deductions()] == ["OTHER_DED"]

    def test_gratuity_is_flagged_not_computed(self, heads):
        r = calculate_fnf(fnf_input(), heads)
        assert (r.service_years, r.service_days) == (6, 258)
        assert "Gratuity NOT CALCULATED" in r.gratuity_note
        assert any("company policy" in w for w in r.warnings)

    @pytest.mark.parametrize(("kw", "message"), [
        ({"lwd": date(2019, 1, 1)}, "LWD before DOJ"),
        ({"notice_days_served": -1}, "negative"),
        ({"encashment_heads": ["NOPE"]}, "Unknown F&F heads"),
    ])  # fmt: skip
    def test_invalid(self, heads, kw, message):
        with pytest.raises(CalculationError, match=message):
            calculate_fnf(fnf_input(**kw), heads)

    @pytest.mark.parametrize(("doj", "lwd", "expected"), [
        (date(2020, 1, 6), date(2025, 1, 5), (4, 366)),
        (date(2020, 1, 6), date(2025, 1, 6), (5, 1)),
        (date(2020, 2, 29), date(2021, 3, 1), (1, 2)),
    ])  # fmt: skip
    def test_service_length(self, doj, lwd, expected):
        assert service_length(doj, lwd) == expected
