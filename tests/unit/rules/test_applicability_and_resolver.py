from dataclasses import dataclass, field
from datetime import date

import pytest

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import (
    AmbiguousRuleError,
    ApplicabilityError,
    MidPeriodRuleChangeError,
    RuleNotFoundError,
    UnverifiedRuleError,
)
from payroll_copilot.domain.enums import Gender, RuleStatus
from payroll_copilot.rules.applicability import applies_to, condition_matches, describe
from payroll_copilot.rules.resolver import resolve_rule

SEP = PayrollPeriod(2026, 9)
AUG = PayrollPeriod(2026, 8)
PF_CTX = {"employee_code": "E1", "pf_applicable": True, "pf_join_date": date(2022, 4, 1)}
PF_ONLY = [{"field": "pf_applicable", "op": "eq", "value": True}]


@dataclass
class FakeRule:
    rule_code: str
    version: int
    effective_from: date
    effective_to: date | None = None
    status: RuleStatus = RuleStatus.VERIFIED
    applicability: list = field(default_factory=list)


OLD = FakeRule("EPF", 1, date(2018, 6, 1), date(2026, 8, 31), applicability=PF_ONLY)
NEW = FakeRule("EPF", 2, date(2026, 9, 1), applicability=PF_ONLY)


class TestApplicability:
    @pytest.mark.parametrize(
        ("condition", "expected"),
        [
            ({"field": "pf_applicable", "op": "eq", "value": True}, True),
            ({"field": "pf_applicable", "op": "ne", "value": True}, False),
            ({"field": "pf_join_date", "op": "gte", "value": "2022-04-01"}, True),
            ({"field": "pf_join_date", "op": "gt", "value": "2022-04-01"}, False),
            ({"field": "pf_join_date", "op": "lt", "value": date(2026, 9, 1)}, True),
            ({"field": "state", "op": "in", "value": ["MH", "KA"]}, True),
            ({"field": "state", "op": "not_in", "value": ["MH"]}, False),
            ({"field": "gender", "op": "eq", "value": "F"}, True),
            ({"field": "lwd", "op": "gte", "value": "2026-09-01"}, False),
            ({"field": "lwd", "op": "eq", "value": None}, True),
        ],
    )
    def test_operators(self, condition, expected):
        ctx = {**PF_CTX, "state": "MH", "gender": Gender.FEMALE, "lwd": None}
        assert condition_matches(condition, ctx) is expected

    def test_empty_list_applies_to_all(self):
        assert applies_to([], {}) is True
        assert describe([]) == "applies to all"

    def test_missing_field_is_error_not_silent_mismatch(self):
        with pytest.raises(ApplicabilityError, match="no field 'esic_applicable'"):
            condition_matches({"field": "esic_applicable", "op": "eq", "value": True}, PF_CTX)

    def test_unknown_operator(self):
        with pytest.raises(ApplicabilityError, match="Unsupported operator"):
            condition_matches({"field": "pf_applicable", "op": "like", "value": 1}, PF_CTX)

    def test_incomparable_values(self):
        with pytest.raises(ApplicabilityError, match="Cannot compare"):
            condition_matches({"field": "pf_join_date", "op": "gt", "value": 5}, PF_CTX)


class TestResolver:
    def test_old_rule_for_august_new_rule_for_september(self):
        assert resolve_rule([OLD, NEW], AUG, PF_CTX, rule_type="PF").rule is OLD
        resolved = resolve_rule([OLD, NEW], SEP, PF_CTX, rule_type="PF")
        assert resolved.rule is NEW
        assert resolved.reference == "EPF v2 (2026-09-01 to open)"
        assert resolved.warnings == []

    def test_no_version_in_period(self):
        with pytest.raises(RuleNotFoundError, match="no version is effective"):
            resolve_rule([OLD], SEP, PF_CTX, rule_type="PF")

    def test_applicability_mismatch_explained(self):
        ctx = {**PF_CTX, "pf_applicable": False}
        with pytest.raises(RuleNotFoundError, match=r"EPF v2 \[pf_applicable eq True\]"):
            resolve_rule([OLD, NEW], SEP, ctx, rule_type="PF")

    def test_overlapping_matches_are_ambiguous(self):
        clash = FakeRule("EPF-X", 1, date(2026, 1, 1), applicability=PF_ONLY)
        with pytest.raises(AmbiguousRuleError, match="2 PF rules apply"):
            resolve_rule([NEW, clash], SEP, PF_CTX, rule_type="PF")

    def test_mid_month_change_requires_split(self):
        first = FakeRule("R", 1, date(2026, 1, 1), date(2026, 9, 14))
        second = FakeRule("R", 2, date(2026, 9, 15))
        with pytest.raises(MidPeriodRuleChangeError) as exc_info:
            resolve_rule([second, first], SEP, PF_CTX, rule_type="PF")
        assert exc_info.value.rules == [first, second]

    def test_partial_coverage_warns(self):
        late = FakeRule("R", 1, date(2026, 9, 10))
        resolved = resolve_rule([late], SEP, PF_CTX, rule_type="PF")
        assert "covers only part of 2026-09" in resolved.warnings[0]

    def test_retired_rules_ignored(self):
        retired = FakeRule("EPF", 3, date(2026, 9, 1), status=RuleStatus.RETIRED)
        assert resolve_rule([NEW, retired], SEP, PF_CTX, rule_type="PF").rule is NEW

    def test_unverified_warns_or_fails(self):
        draft = FakeRule("EPF", 2, date(2026, 9, 1), status=RuleStatus.UNVERIFIED)
        resolved = resolve_rule([draft], SEP, PF_CTX, rule_type="PF")
        assert "UNVERIFIED" in resolved.warnings[0]
        with pytest.raises(UnverifiedRuleError):
            resolve_rule([draft], SEP, PF_CTX, rule_type="PF", allow_unverified=False)
