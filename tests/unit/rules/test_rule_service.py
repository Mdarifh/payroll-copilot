"""Rule resolution against the real repo rules and demo employees."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import (
    MidPeriodRuleChangeError,
    RuleNotFoundError,
    UnverifiedRuleError,
)
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


def _resolve_ctx(session, rule_type, code, **overrides):
    ctx = {**_ctx(session, code), **overrides}
    return RuleService(session, allow_unverified=True).resolve(rule_type, SEP, ctx)


def test_pf_old_ceiling_in_august_new_from_17_september(seeded_session):
    aug = _resolve(seeded_session, RuleType.PF, "EMP-101", AUG).rule
    oct_ = _resolve(seeded_session, RuleType.PF, "EMP-101", PayrollPeriod(2026, 10)).rule
    assert (aug.version, aug.pf_wage_ceiling) == (1, Decimal("15000"))
    assert (oct_.version, oct_.pf_wage_ceiling) == (2, Decimal("25000"))
    # S.O. 5109(E) takes effect on 17-Sep: one rule cannot cover September - the segment
    # engine (calculations/pf_segments) splits it; the single-rule resolver refuses
    with pytest.raises(MidPeriodRuleChangeError, match="split the period"):
        _resolve(seeded_session, RuleType.PF, "EMP-101", SEP)


def test_no_pf_rule_for_excluded_employee(seeded_session):
    with pytest.raises(RuleNotFoundError, match="EMP-101"):
        _resolve_ctx(seeded_session, RuleType.PF, "EMP-101", pf_applicable=False)


def test_esic_only_for_covered_employees(seeded_session):
    assert _resolve(seeded_session, RuleType.ESIC, "EMP-103").rule.employee_rate == Decimal("0.75")
    with pytest.raises(RuleNotFoundError):
        _resolve(seeded_session, RuleType.ESIC, "EMP-101")


@pytest.mark.parametrize(
    ("code", "state", "rule_code", "levied"),
    [("EMP-101", None, "PT-MH", True), ("EMP-102", None, "PT-KA", True),
     ("EMP-104", None, "PT-TN", True), ("EMP-105", None, "PT-TG", True),
     ("EMP-101", "DL", "PT-DL", False), ("EMP-101", "HR", "PT-HR", False)],
)  # fmt: skip
def test_pt_by_work_state(seeded_session, code, state, rule_code, levied):
    extra = {"work_state": state} if state else {}
    rule = _resolve_ctx(seeded_session, RuleType.PT, code, **extra).rule
    assert (rule.rule_code, rule.is_levied) == (rule_code, levied)


def test_pt_mh_has_female_slabs_and_feb_override(seeded_session):
    rule = _resolve(seeded_session, RuleType.PT, "HR-001").rule
    female = [s for s in rule.slabs if s.gender and s.gender.value == "F"]
    assert female[0].upper_inclusive == Decimal("25000")
    assert female[-1].month_overrides == {"2": "300"}


def test_lwf_state_and_missing_state(seeded_session):
    assert _resolve(seeded_session, RuleType.LWF, "EMP-102").rule.rule_code == "LWF-KA"
    with pytest.raises(RuleNotFoundError, match="no version is effective"):
        _resolve_ctx(seeded_session, RuleType.LWF, "EMP-102", work_state="PB")


def test_tds_regime_and_age(seeded_session):
    assert _resolve(seeded_session, RuleType.TDS, "EMP-104").rule.rule_code == "TDS-OLD"
    senior = _resolve_ctx(seeded_session, RuleType.TDS, "EMP-104", age_category="SENIOR").rule
    assert senior.rule_code == "TDS-OLD-SENIOR"
    new = _resolve(seeded_session, RuleType.TDS, "EMP-101").rule
    assert (new.rule_code, new.standard_deduction) == ("TDS-NEW", Decimal("75000"))


def test_tds_outside_configured_fy(seeded_session):
    with pytest.raises(RuleNotFoundError):
        _resolve(seeded_session, RuleType.TDS, "EMP-101", PayrollPeriod(2027, 4))


def test_unverified_blocked_when_disallowed(seeded_session):
    service = RuleService(seeded_session, allow_unverified=False)
    with pytest.raises(UnverifiedRuleError):
        service.resolve(RuleType.PF, AUG, _ctx(seeded_session, "EMP-101", AUG))


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
