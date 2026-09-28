from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError

from payroll_copilot.core.exceptions import MoneyError
from payroll_copilot.db.models import Attendance, Company, Employee, PFRule
from payroll_copilot.domain.enums import Gender


def _company(session) -> Company:
    company = Company(code="C1", name="Test Co", state_code="MH")
    session.add(company)
    session.flush()
    return company


def _employee(company, code="E1", **overrides) -> Employee:
    values = dict(
        company=company, employee_code=code, full_name="Test", gender=Gender.MALE,
        dob=date(1990, 1, 1), doj=date(2020, 1, 1), work_state="MH",
    )  # fmt: skip
    values.update(overrides)
    return Employee(**values)


def _pf_rule(**overrides) -> PFRule:
    values = dict(
        rule_code="EPF", version=1, name="t", effective_from=date(2020, 1, 1),
        content_hash="x", employee_rate=Decimal("12"), employer_rate=Decimal("12"),
        eps_rate=Decimal("8.33"), edli_rate=Decimal("0.5"), admin_rate=Decimal("0.5"),
        pf_wage_ceiling=Decimal("15000"), eps_wage_ceiling=Decimal("15000"),
        edli_wage_ceiling=Decimal("15000"),
    )  # fmt: skip
    values.update(overrides)
    return PFRule(**values)


class TestDecimalType:
    def test_roundtrip_is_exact(self, session):
        session.add(_pf_rule(eps_rate=Decimal("8.3333"), admin_min_amount=Decimal("0.10")))
        session.commit()
        session.expire_all()
        rule = session.scalar(select(PFRule))
        assert rule.eps_rate == Decimal("8.3333")
        assert rule.admin_min_amount == Decimal("0.10")
        assert isinstance(rule.pf_wage_ceiling, Decimal)
        raw = session.execute(text("SELECT eps_rate FROM pf_rules")).scalar()
        assert raw == "8.3333"  # stored as exact text on SQLite

    def test_float_rejected_on_write(self, session):
        session.add(_pf_rule(employee_rate=12.0))
        with pytest.raises(StatementError) as exc_info:
            session.flush()
        assert isinstance(exc_info.value.orig, MoneyError)


class TestConstraints:
    def test_employee_code_unique(self, session):
        company = _company(session)
        session.add_all([_employee(company, "E1"), _employee(company, "E1")])
        with pytest.raises(IntegrityError):
            session.flush()

    def test_lwd_not_before_doj(self, session):
        company = _company(session)
        session.add(_employee(company, lwd=date(2019, 12, 31)))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_foreign_keys_enforced_on_sqlite(self, session):
        session.add(
            Attendance(
                employee_id=999, period="2026-09", calendar_days=30, working_days=26,
                week_offs=4, holidays=0, present_days=26,
            )  # fmt: skip
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_rule_code_version_unique(self, session):
        session.add_all([_pf_rule(), _pf_rule()])
        with pytest.raises(IntegrityError):
            session.flush()

    def test_rule_range_check(self, session):
        session.add(_pf_rule(effective_to=date(2019, 1, 1)))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_invalid_enum_string_rejected(self, session):
        company = _company(session)
        session.add(_employee(company, gender="X"))
        with pytest.raises(StatementError):
            session.flush()
