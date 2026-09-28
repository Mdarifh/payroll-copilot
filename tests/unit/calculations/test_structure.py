from datetime import date
from decimal import Decimal

import pytest

from payroll_copilot.calculations.structure import (
    check_assignment_against_template,
    compute_structure_breakup,
)
from payroll_copilot.core.exceptions import StructureError
from payroll_copilot.core.money import RoundingMode
from payroll_copilot.domain.enums import AssignmentReason, ComparisonStatus
from payroll_copilot.domain.salary import PayLine, SalaryAssignmentInfo, StructureTemplate

D = Decimal


def _template(*components) -> StructureTemplate:
    return StructureTemplate(code="T", version=1, name="t", components=list(components))


class TestBreakup:
    @pytest.mark.parametrize(
        ("structure", "gross", "expected"),
        [
            ("STD-2026", 60000, {"BASIC": 30000, "HRA": 15000, "CONVEYANCE": 1600, "MEDICAL": 1250, "SPECIAL": 12150}),
            ("STD-2026", 35000, {"BASIC": 17500, "HRA": 8750, "CONVEYANCE": 1600, "MEDICAL": 1250, "SPECIAL": 5900}),
            ("LOW-2026", 18000, {"BASIC": 10800, "HRA": 4320, "SPECIAL": 2880}),
            ("LOW-2026", 20500, {"BASIC": 12300, "HRA": 4920, "SPECIAL": 3280}),
        ],
    )
    def test_seed_structures(self, templates, heads, structure, gross, expected):
        result = compute_structure_breakup(templates[structure], gross, heads)
        assert result.breakdown == {k: D(v) for k, v in expected.items()}
        assert result.value == D(gross)

    def test_rounding_is_absorbed_by_balancing_head(self, templates, heads):
        result = compute_structure_breakup(templates["STD-2026"], "33333", heads)
        assert result.breakdown["BASIC"] == D("16667")  # 16666.5 half-up
        assert result.breakdown["HRA"] == D("8334")  # 8333.5 half-up
        assert result.breakdown["SPECIAL"] == D("5482")
        assert sum(result.breakdown.values()) == D("33333")
        assert "raw 16666.5" in result.steps[0].note

    def test_rounding_mode_is_configurable(self, templates, heads):
        result = compute_structure_breakup(
            templates["STD-2026"], "33333", heads, rounding_mode=RoundingMode.FLOOR
        )
        assert result.breakdown["BASIC"] == D("16666")
        assert sum(result.breakdown.values()) == D("33333")

    def test_breakdown_follows_template_sequence(self, templates, heads):
        result = compute_structure_breakup(templates["STD-2026"], 60000, heads)
        assert list(result.breakdown) == ["BASIC", "HRA", "CONVEYANCE", "MEDICAL", "SPECIAL"]

    def test_trace_text(self, templates, heads):
        text = compute_structure_breakup(templates["STD-2026"], 60000, heads).to_text()
        assert "Basic: Gross 60000 × 50% = ₹30,000" in text
        assert "Special Allowance: Gross 60000 − other components 47850 = ₹12,150" in text

    def test_fixed_components_exceed_gross(self, templates, heads):
        with pytest.raises(StructureError, match="more than gross"):
            compute_structure_breakup(templates["STD-2026"], 2000, heads)

    def test_zero_balancing_warns(self, heads):
        template = _template(
            {"head_code": "BASIC", "calc_type": "PERCENT_OF_GROSS", "value": 100, "sequence": 1},
            {"head_code": "SPECIAL", "calc_type": "BALANCING", "sequence": 2},
        )
        result = compute_structure_breakup(template, 10000, heads)
        assert result.breakdown["SPECIAL"] == 0
        assert "is zero" in result.warnings[0]

    @pytest.mark.parametrize(
        ("components", "message"),
        [
            ([{"head_code": "NOPE", "calc_type": "FIXED", "value": 1}], "unknown salary head"),
            ([{"head_code": "EE_PF", "calc_type": "FIXED", "value": 1}], "is a DEDUCTION"),
            ([{"head_code": "BASIC", "calc_type": "BALANCING"}, {"head_code": "HRA", "calc_type": "BALANCING"}], "only one BALANCING"),
            ([{"head_code": "BASIC", "calc_type": "FIXED"}], "non-negative value"),
            ([{"head_code": "HRA", "calc_type": "PERCENT_OF_HEAD", "value": 40}], "without base head"),
            ([{"head_code": "HRA", "calc_type": "PERCENT_OF_HEAD", "value": 40, "base_head_code": "BASIC", "sequence": 1},
              {"head_code": "BASIC", "calc_type": "PERCENT_OF_GROSS", "value": 50, "sequence": 2}], "lower sequence"),
            ([{"head_code": "BASIC", "calc_type": "FIXED", "value": 5000}], "no BALANCING head"),
            ([{"head_code": "BASIC", "calc_type": "FIXED", "value": 1}, {"head_code": "BASIC", "calc_type": "FIXED", "value": 2}], "appears twice"),
        ],
    )
    def test_invalid_templates(self, heads, components, message):
        with pytest.raises(StructureError, match=message):
            compute_structure_breakup(_template(*components), 10000, heads)

    def test_template_without_balancing_must_sum_to_gross(self, heads):
        template = _template({"head_code": "BASIC", "calc_type": "FIXED", "value": 10000})
        assert compute_structure_breakup(template, 10000, heads).value == D(10000)

    @pytest.mark.parametrize("gross", [0, -100])
    def test_gross_must_be_positive(self, templates, heads, gross):
        with pytest.raises(StructureError, match="positive"):
            compute_structure_breakup(templates["STD-2026"], gross, heads)


class TestTemplateCheck:
    def _assignment(self, template, components) -> SalaryAssignmentInfo:
        return SalaryAssignmentInfo(
            structure=template,
            monthly_gross=D(sum(components.values())),
            effective_from=date(2026, 4, 1),
            reason=AssignmentReason.REVISION,
            components=[PayLine(head_code=k, amount=D(v)) for k, v in components.items()],
        )

    def test_consistent(self, templates, heads):
        a = self._assignment(
            templates["LOW-2026"], {"BASIC": 10800, "HRA": 4320, "SPECIAL": 2880}
        )
        assert check_assignment_against_template(a, heads).is_consistent

    def test_mismatch_and_missing(self, templates, heads):
        a = self._assignment(
            templates["LOW-2026"], {"BASIC": 10000, "HRA": 4320, "SPECIAL": 3680, "BONUS": 0}
        )
        check = check_assignment_against_template(a, heads)
        status = {line.head_code: line for line in check.lines}
        assert not check.is_consistent
        assert status["BASIC"].status is ComparisonStatus.MISMATCH
        assert status["BASIC"].difference == D(-800)
        assert status["BONUS"].status is ComparisonStatus.MISSING_EXPECTED

    def test_missing_actual(self, templates, heads):
        a = self._assignment(templates["LOW-2026"], {"BASIC": 10800, "SPECIAL": 7200})
        status = {line.head_code: line.status for line in check_assignment_against_template(a, heads).lines}
        assert status["HRA"] is ComparisonStatus.MISSING_ACTUAL
