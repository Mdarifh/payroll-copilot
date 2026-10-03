import copy
from decimal import Decimal

import pytest
from sqlalchemy import select

from payroll_copilot.core.exceptions import RuleConfigError, RuleImmutableError
from payroll_copilot.db.models import PFRule, PTRule
from payroll_copilot.domain.enums import RuleStatus
from payroll_copilot.rules.loader import (
    load_rule_files,
    parse_rule_file,
    read_yaml,
    sync_rules,
)
from tests.conftest import RULES_DIR

PF_FILE = {
    "rule_type": "PF",
    "rule_code": "EPF-T",
    "versions": [
        {
            "version": 1, "name": "old", "effective_from": "2018-06-01",
            "effective_to": "2026-08-31",
            "employee_rate": 12, "employer_rate": 12, "eps_rate": "8.33", "edli_rate": "0.5",
            "admin_rate": "0.5", "pf_wage_ceiling": 15000, "eps_wage_ceiling": 15000,
            "edli_wage_ceiling": 15000,
        },
        {
            "version": 2, "name": "new", "effective_from": "2026-09-01",
            "employee_rate": 12, "employer_rate": 12, "eps_rate": "8.33", "edli_rate": "0.5",
            "admin_rate": "0.5", "pf_wage_ceiling": 25000, "eps_wage_ceiling": 25000,
            "edli_wage_ceiling": 25000,
        },
    ],
}  # fmt: skip

PT_FILE = {
    "rule_type": "PT",
    "rule_code": "PT-XX",
    "state_code": "XX",
    "state_name": "Test State",
    "versions": [
        {
            "version": 1, "name": "pt", "effective_from": "2025-04-01",
            "slab_sets": [
                {"gender": None, "slabs": [{"upto": 10000, "amount": 0}, {"upto": None, "amount": 200}]}
            ],
        }
    ],
}  # fmt: skip


def _with(base: dict, path: list, value) -> dict:
    data = copy.deepcopy(base)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


class TestSchemaValidation:
    def test_valid_files_parse(self):
        assert parse_rule_file(PF_FILE).rule_code == "EPF-T"
        assert parse_rule_file(PT_FILE).versions[0].effective_deduction_months == list(range(1, 13))

    @pytest.mark.parametrize(
        ("path", "value", "message"),
        [
            (["versions", 0, "eps_rate"], "13", "eps_rate cannot exceed"),
            (["versions", 0, "employee_rate"], "120", "less than or equal to 100"),
            (["versions", 0, "effective_to"], "2026-09-15", "overlaps version 2"),
            (["versions", 1, "version"], 1, "unique and increasing"),
            (["versions", 0, "unknown_field"], 1, "Extra inputs"),
            (["versions", 0, "applicability"], [{"field": "x", "op": "like", "value": 1}], "unsupported operator"),
            (["rule_code"], "bad code", "String should match"),
        ],
    )  # fmt: skip
    def test_invalid_pf(self, path, value, message):
        with pytest.raises(RuleConfigError, match=message):
            parse_rule_file(_with(PF_FILE, path, value))

    @pytest.mark.parametrize(
        ("slabs", "message"),
        [
            ([{"upto": 10000, "amount": 0}], "last slab must have upto: null"),
            ([{"upto": 10000, "amount": 0}, {"upto": 5000, "amount": 1}, {"upto": None, "amount": 2}], "strictly increasing"),
            ([{"upto": None, "amount": 0}, {"upto": None, "amount": 2}], "only the last slab"),
            ([{"upto": 1, "amount": -5}, {"upto": None, "amount": 2}], "greater than or equal to 0"),
        ],
    )  # fmt: skip
    def test_invalid_pt_slabs(self, slabs, message):
        data = _with(PT_FILE, ["versions", 0, "slab_sets", 0, "slabs"], slabs)
        with pytest.raises(RuleConfigError, match=message):
            parse_rule_file(data)

    def test_pt_not_levied_cannot_have_slabs(self):
        data = _with(PT_FILE, ["versions", 0, "is_levied"], False)
        with pytest.raises(RuleConfigError, match="must not define slab_sets"):
            parse_rule_file(data)

    def test_pt_half_yearly_needs_deduction_months(self):
        data = _with(PT_FILE, ["versions", 0, "frequency"], "HALF_YEARLY")
        with pytest.raises(RuleConfigError, match="requires deduction_months"):
            parse_rule_file(data)

    def test_pt_requires_default_gender_set(self):
        data = _with(PT_FILE, ["versions", 0, "slab_sets", 0, "gender"], "F")
        with pytest.raises(RuleConfigError, match="gender: null"):
            parse_rule_file(data)

    def test_lwf_needs_exactly_one_mode(self):
        data = {
            "rule_type": "LWF", "rule_code": "LWF-XX", "state_code": "XX", "state_name": "X",
            "versions": [{
                "version": 1, "name": "l", "effective_from": "2025-04-01",
                "frequency": "YEARLY", "deduction_months": [12],
                "employee_amount": 1, "employer_amount": 2, "employee_rate": 1,
                "employer_multiplier": 2,
            }],
        }  # fmt: skip
        with pytest.raises(RuleConfigError, match="either"):
            parse_rule_file(data)

    def test_tds_dates_must_match_fy(self):
        data = read_yaml(RULES_DIR / "tds" / "tds_new_regime.yaml")
        data["versions"][0]["effective_to"] = "2027-02-28"
        with pytest.raises(RuleConfigError, match="must equal FY 2026-27"):
            parse_rule_file(data)

    def test_yaml_floats_are_exact_decimals(self):
        data = read_yaml(RULES_DIR / "pf" / "epf.yaml")
        assert data["versions"][0]["eps_rate"] == Decimal("8.33")
        assert isinstance(data["versions"][0]["eps_rate"], Decimal)


class TestRepoRuleFiles:
    def test_all_repo_rule_files_are_valid(self):
        files = load_rule_files(RULES_DIR)
        assert {f.rule_type for f in files} == {"PF", "ESIC", "PT", "LWF", "TDS"}

    def test_pt_covers_all_configured_states(self):
        pt = {f.state_code: f for f in load_rule_files(RULES_DIR) if f.rule_type == "PT"}
        levying = {
            "MH", "KA", "WB", "TN", "AP", "TG", "GJ", "MP", "KL", "OD", "AS", "ML", "TR",
            "SK", "MZ", "MN", "NL", "BR", "JH", "PB", "PY",
        }  # fmt: skip
        not_levied = {"DL", "UP", "HR", "RJ", "HP", "UK", "CH"}
        assert set(pt) == levying | not_levied
        for code in levying:
            assert pt[code].versions[-1].is_levied, code
        for code in not_levied:
            assert not pt[code].versions[-1].is_levied, code

    def test_state_file_names_match_state_code(self):
        for path in (RULES_DIR / "pt").glob("*.yaml"):
            assert read_yaml(path)["state_code"] == path.stem

    def test_new_pf_rule_is_25000_from_17_sep_2026(self):
        epf = next(f for f in load_rule_files(RULES_DIR) if f.rule_code == "EPF")
        new = epf.versions[-1]
        assert str(new.effective_from) == "2026-09-17"  # S.O. 5109(E)
        assert str(epf.versions[-2].effective_to) == "2026-09-16"
        assert new.pf_wage_ceiling == Decimal("25000")
        assert epf.versions[-2].pf_wage_ceiling == Decimal("15000")

    def test_duplicate_rule_code_across_files(self, tmp_path):
        import yaml

        for name in ("a.yaml", "b.yaml"):
            (tmp_path / name).write_text(yaml.safe_dump(PT_FILE), encoding="utf-8")
        with pytest.raises(RuleConfigError, match="defined in both"):
            load_rule_files(tmp_path)

    def test_missing_dir(self, tmp_path):
        with pytest.raises(RuleConfigError, match="not found"):
            load_rule_files(tmp_path / "nope")


class TestSync:
    def test_insert_then_unchanged(self, session):
        files = [parse_rule_file(PF_FILE), parse_rule_file(PT_FILE)]
        first = sync_rules(session, files)
        assert first.inserted == ["EPF-T v1", "EPF-T v2", "PT-XX v1"]
        second = sync_rules(session, files)
        assert second.unchanged == first.inserted and not second.inserted

        pt = session.scalar(select(PTRule))
        assert [(s.lower_exclusive, s.upper_inclusive, s.amount) for s in pt.slabs] == [
            (None, Decimal("10000"), Decimal("0")),
            (Decimal("10000"), None, Decimal("200")),
        ]

    def test_unverified_can_be_updated(self, session):
        sync_rules(session, [parse_rule_file(PF_FILE)])
        changed = _with(PF_FILE, ["versions", 1, "eps_wage_ceiling"], 15000)
        report = sync_rules(session, [parse_rule_file(changed)])
        assert report.updated == ["EPF-T v2"]
        rule = session.scalar(select(PFRule).where(PFRule.version == 2))
        assert rule.eps_wage_ceiling == Decimal("15000")

    def test_verified_values_are_immutable(self, session):
        verified = _with(PF_FILE, ["versions", 1, "status"], "VERIFIED")
        sync_rules(session, [parse_rule_file(verified)])
        changed = _with(verified, ["versions", 1, "pf_wage_ceiling"], 30000)
        with pytest.raises(RuleImmutableError, match="EPF-T v2 is VERIFIED"):
            sync_rules(session, [parse_rule_file(changed)])

    def test_status_change_alone_is_allowed(self, session):
        sync_rules(session, [parse_rule_file(PF_FILE)])
        verified = _with(PF_FILE, ["versions", 1, "status"], "VERIFIED")
        report = sync_rules(session, [parse_rule_file(verified)])
        assert "EPF-T v2" in report.unchanged
        rule = session.scalar(select(PFRule).where(PFRule.version == 2))
        assert rule.status is RuleStatus.VERIFIED

    def test_pt_slab_update_replaces_children(self, session):
        sync_rules(session, [parse_rule_file(PT_FILE)])
        slabs = [{"upto": 5000, "amount": 0}, {"upto": 9000, "amount": 100}, {"upto": None, "amount": 250}]
        changed = _with(PT_FILE, ["versions", 0, "slab_sets", 0, "slabs"], slabs)
        sync_rules(session, [parse_rule_file(changed)])
        session.expire_all()
        pt = session.scalar(select(PTRule))
        assert [s.amount for s in pt.slabs] == [Decimal("0"), Decimal("100"), Decimal("250")]
