from datetime import date
from decimal import Decimal

from payroll_copilot.calculations.trace import CalculationResult, RuleRef, TraceStep
from payroll_copilot.core.money import RoundingMode
from payroll_copilot.domain.enums import RuleStatus, RuleType


def _result() -> CalculationResult:
    rule = RuleRef(
        rule_type=RuleType.PF, rule_code="EPF", version=2, effective_from=date(2026, 9, 1),
        status=RuleStatus.UNVERIFIED,
    )  # fmt: skip
    return CalculationResult(
        component="EE_PF",
        employee_code="ARIF-112",
        period="2026-09",
        inputs={"basic": Decimal("30000")},
        rule=rule,
        formula="PF Wage × employee rate",
        steps=[
            TraceStep(label="PF Wage", formula="min(30000, 25000)", result=Decimal("25000")),
            TraceStep(label="Employee PF", formula="25000 × 12%", result=Decimal("3000")),
        ],
        raw_value=Decimal("3000.00"),
        value=Decimal("3000"),
        rounding_mode=RoundingMode.HALF_UP,
        warnings=["EPF v2 is UNVERIFIED"],
    )


def test_text_rendering():
    text = _result().to_text()
    assert text.splitlines()[0] == "EE_PF | ARIF-112 | 2026-09"
    assert "Rule: EPF v2 (2026-09-01 to open) [UNVERIFIED]" in text
    assert "1. PF Wage: min(30000, 25000) = ₹25,000" in text
    assert "Result: ₹3,000" in text
    assert "WARNING: EPF v2 is UNVERIFIED" in text


def test_json_roundtrip_keeps_exact_decimals():
    data = _result().to_json_dict()
    assert data["value"] == "3000"
    assert data["rule"]["effective_from"] == "2026-09-01"
    restored = CalculationResult.model_validate(data)
    # typed fields come back exactly; free-form ``inputs`` stay JSON strings
    assert restored.value == Decimal("3000") and restored.raw_value == Decimal("3000.00")
    assert restored.steps == _result().steps
    assert restored.rule == _result().rule
    assert restored.inputs == {"basic": "30000"}


def test_rule_ref_from_orm_like_object():
    class Row:
        rule_code, version, effective_from, effective_to = "ESIC", 1, date(2019, 7, 1), None
        status = RuleStatus.VERIFIED

    ref = RuleRef.from_rule(Row(), RuleType.ESIC)
    assert ref.label == "ESIC v1 (2019-07-01 to open)"
