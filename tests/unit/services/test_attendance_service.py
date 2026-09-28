from decimal import Decimal

import pytest

from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.domain.enums import ComparisonStatus, PaidDaysBasis
from payroll_copilot.repositories.employee_repo import EmployeeRepository
from payroll_copilot.services.attendance_service import AttendanceService

SEP = PayrollPeriod(2026, 9)

EXPECTED_SEP_PAID_DAYS = {
    "ARIF-112": 30, "EMP-002": 27, "EMP-003": 30, "EMP-004": 30, "EMP-005": 16,
    "EMP-006": 20, "EMP-007": 30, "EMP-008": 28, "EMP-009": 30, "EMP-010": 29,
}  # fmt: skip


@pytest.mark.parametrize(("code", "expected"), EXPECTED_SEP_PAID_DAYS.items())
def test_seed_paid_days_september(seeded_session, code, expected):
    view = AttendanceService(seeded_session).get_attendance(code, SEP)
    assert view.paid_days.value == Decimal(expected)
    assert view.paid_days.warnings == [], view.paid_days.warnings
    assert view.basis is PaidDaysBasis.CALENDAR


@pytest.mark.parametrize(("code", "expected"), [("EMP-004", 31), ("EMP-010", 30)])
def test_seed_paid_days_august(seeded_session, code, expected):
    view = AttendanceService(seeded_session).get_attendance(code, PayrollPeriod(2026, 8))
    assert view.paid_days.value == Decimal(expected)


def test_missing_attendance(seeded_session):
    with pytest.raises(NotFoundError, match="No attendance for EMP-005 in 2026-08"):
        AttendanceService(seeded_session).get_attendance("EMP-005", PayrollPeriod(2026, 8))


def test_employee_basis_override(seeded_session):
    employee = EmployeeRepository(seeded_session).get_by_code("EMP-002")
    employee.paid_days_basis = PaidDaysBasis.FIXED_26
    seeded_session.flush()
    view = AttendanceService(seeded_session).get_attendance("EMP-002", SEP)
    assert view.basis is PaidDaysBasis.FIXED_26
    assert view.paid_days.value == Decimal(23)  # 26 − 3 LOP


def test_diagnose(seeded_session):
    d = AttendanceService(seeded_session).diagnose_paid_days("EMP-006", SEP, Decimal(30))
    assert d.status is ComparisonStatus.MISMATCH
    assert "10 days before DOJ / after LWD not excluded" in d.causes[0]


def test_text_view(seeded_session):
    text = AttendanceService(seeded_session).get_attendance("EMP-010", SEP).to_text()
    assert "Paid days 29 of 30" in text
    assert "LOP reversal 1" in text
    assert "₹" not in text
