"""Rule resolution against the real repo rules and demo employees."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import RuleNotFoundError, UnverifiedRuleError
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import AgeCategory, RuleType
from payroll_copilot.rules.context import build_employee_context, tax_age_category
from payroll_copilot.rules.service import RuleService

SEP = PayrollPeriod(2026, 9)
AUG = PayrollPeriod(2026, 8)


def _ctx(session, code, period=SEP):
    employee = session.scalar(select(Employee).where(Employee.employee_code == code))
    return build_employee_context(employee, period)


def _resolve(session, rule_type, code, period=SEP):
    return RuleService(session, allow_unverified=True).resolve(
        rule_type, period, _ctx(session, code, period)
    )


def test_pf_old_ceiling_in_august_new_in_september(seeded_session):
    aug = _resolve(seeded_session, RuleType.PF, "ARIF-112", AUG).rule
    sep = _resolve(seeded_session, RuleType.PF, "ARIF-112", SEP).rule
    assert (aug.version, aug.pf_wage_ceiling) == (1, Decimal("15000"))
    assert (sep.version, sep.pf_wage_ceiling) == (2, Decimal("25000"))


def test_no_pf_rule_for_excluded_employee(seeded_session):
    with pytest.raises(RuleNotFoundError, match="EMP-007"):
        _resolve(seeded_session, RuleType.PF, "EMP-007")


def test_esic_only_for_covered_employees(seeded_session):
    assert _resolve(seeded_session, RuleType.ESIC, "EMP-003").rule.employee_rate == Decimal("0.75")
    with pytest.raises(RuleNotFoundError):
        _resolve(seeded_session, RuleType.ESIC, "ARIF-112")


@pytest.mark.parametrize(
    ("code", "rule_code", "levied"),
    [("EMP-010", "PT-MH", True), ("EMP-002", "PT-KA", True), ("EMP-004", "PT-TN", True),
     ("EMP-005", "PT-DL", False), ("EMP-009", "PT-HR", False)],
)  # fmt: skip
def test_pt_by_work_state(seeded_session, code, rule_code, levied):
    rule = _resolve(seeded_session, RuleType.PT, code).rule
    assert (rule.rule_code, rule.is_levied) == (rule_code, levied)


def test_pt_mh_has_female_slabs_and_feb_override(seeded_session):
    rule = _resolve(seeded_session, RuleType.PT, "EMP-010").rule
    female = [s for s in rule.slabs if s.gender and s.gender.value == "F"]
    assert female[0].upper_inclusive == Decimal("25000")
    assert female[-1].month_overrides == {"2": "300"}


def test_lwf_state_and_missing_state(seeded_session):
    assert _resolve(seeded_session, RuleType.LWF, "EMP-009").rule.rule_code == "LWF-HR"
    ctx = {**_ctx(seeded_session, "EMP-009"), "work_state": "PB"}
    with pytest.raises(RuleNotFoundError, match="no version is effective"):
        RuleService(seeded_session).resolve(RuleType.LWF, SEP, ctx)


def test_tds_regime_and_age(seeded_session):
    assert _resolve(seeded_session, RuleType.TDS, "EMP-009").rule.rule_code == "TDS-OLD-SENIOR"
    new = _resolve(seeded_session, RuleType.TDS, "ARIF-112").rule
    assert (new.rule_code, new.standard_deduction) == ("TDS-NEW", Decimal("75000"))


def test_tds_outside_configured_fy(seeded_session):
    with pytest.raises(RuleNotFoundError):
        _resolve(seeded_session, RuleType.TDS, "ARIF-112", PayrollPeriod(2027, 4))


def test_unverified_blocked_when_disallowed(seeded_session):
    service = RuleService(seeded_session, allow_unverified=False)
    with pytest.raises(UnverifiedRuleError):
        service.resolve(RuleType.PF, SEP, _ctx(seeded_session, "ARIF-112"))


@pytest.mark.parametrize(
    ("dob", "expected"),
    [
        (date(1967, 3, 31), AgeCategory.SENIOR),  # turns 60 on FY end 31-Mar-2027
        (date(1967, 4, 1), AgeCategory.GENERAL),
        (date(1947, 3, 31), AgeCategory.SUPER_SENIOR),
        (date(1947, 4, 1), AgeCategory.SENIOR),
    ],
)
def test_tax_age_category_uses_fy_end(dob, expected):
    assert tax_age_category(dob, SEP) is expected
