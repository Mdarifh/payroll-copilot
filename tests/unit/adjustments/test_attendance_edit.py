"""Manual attendance correction and the approval on/off switch."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Attendance, AuditLog, Employee
from payroll_copilot.domain.enums import AttendanceStatus
from payroll_copilot.services import action_center as ac
from payroll_copilot.services.pay_adjustments import AdjustmentError
from payroll_copilot.services.simulation import PayrollChange, simulate
from tests.unit.chat.conftest import FakeChatModel

SEP, OCT = PayrollPeriod(2026, 9), PayrollPeriod(2026, 10)


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def day(s, code, d) -> Attendance:
    return s.scalar(select(Attendance).where(Attendance.employee_id == emp(s, code).id,
                                             Attendance.day == d))


def submit(s, actor, change, period=SEP, code="EMP-101"):
    return ac.submit(s, actor, code, period, change,
                     simulate(s, actor, code, period, change)["preview_id"])


def present_day(s) -> date:
    return next(a.day for a in s.scalars(select(Attendance).where(
        Attendance.employee_id == emp(s, "EMP-101").id, Attendance.status == AttendanceStatus.PRESENT,
        Attendance.day.between(SEP.start, SEP.end))))


def test_mark_absent_with_lop_after_approval(seeded_session):
    s = seeded_session
    maker, checker = emp(s, "HR-001"), emp(s, "HR-002")
    d = present_day(s)
    change = PayrollChange(attendance=[{"date": d, "status": "ABSENT", "remarks": "No show"}],
                           lop_days=Decimal(1), reason="Absent, no leave applied")
    preview = simulate(s, maker, "EMP-101", SEP, change)
    assert preview["requires_approval"] and Decimal(preview["net_change"]) < 0
    out = submit(s, maker, change)
    assert out["status"] == "PENDING_APPROVAL" and out["kind"] == "attendance"
    assert day(s, "EMP-101", d).status is AttendanceStatus.PRESENT  # not yet
    fresh = ac.fresh_preview(s, checker, out["id"])
    assert ac.approve(s, checker, out["id"], fresh["preview_id"])["status"] == "APPLIED"
    assert day(s, "EMP-101", d).status is AttendanceStatus.ABSENT
    assert day(s, "EMP-101", d).remarks == "No show"
    log = s.scalar(select(AuditLog).where(AuditLog.action == "attendance.update"))
    assert log.new_value["changes"][0] == {"date": d.isoformat(), "old": "PRESENT", "new": "ABSENT"}


def test_approval_switch_applies_at_once(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    assert ac.set_approval(s, hr, False, "Single HR team for now")["change_approval"] is False
    d = present_day(s)
    out = submit(s, hr, PayrollChange(attendance=[{"date": d, "status": "HALF_DAY"}],
                                      reason="Left early, half day"))
    assert out["status"] == "APPLIED"
    assert day(s, "EMP-101", d).status is AttendanceStatus.HALF_DAY


def test_create_missing_month_and_rules(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    ac.set_approval(s, hr, False, "Single HR team for now")
    october = PayrollChange(fill_month=True, reason="Create October attendance")
    preview = simulate(s, hr, "EMP-101", OCT, october)
    assert preview["no_attendance_before"]
    submit(s, hr, october, OCT)
    days = s.scalars(select(Attendance).where(Attendance.employee_id == emp(s, "EMP-101").id,
                                              Attendance.day >= OCT.start)).all()
    assert days and all(a.day <= date.today() for a in days)
    assert {a.status for a in days if a.day.weekday() == 6} <= {AttendanceStatus.WEEK_OFF}
    with pytest.raises(AdjustmentError, match="approved leave"):
        simulate(s, hr, "EMP-101", SEP, PayrollChange(
            attendance=[{"date": present_day(s), "status": "LEAVE"}], reason="mark as leave"))
    with pytest.raises(AdjustmentError, match="future"):
        simulate(s, hr, "EMP-101", PayrollPeriod(2099, 1), PayrollChange(
            attendance=[{"date": "2099-01-05", "status": "ABSENT"}], reason="far future"))


def test_settings_api_is_hr_only(seeded_engine, make_settings):
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip

    def auth(email, password):
        token = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    hr, employee = auth("anjali.mehta@example.com", "Hr@12345"), auth(
        "rahul.verma@example.com", "Employee@123")
    setting = client.get("/actions/settings", headers=hr).json()
    assert setting["change_approval"] is True
    # who can approve Anjali's requests: other HR users, never herself
    assert [a["name"] for a in setting["approvers"]] == ["Kavita Rao", "Rohit Kapoor"]
    body = {"enabled": False, "reason": "Single HR team"}
    assert client.put("/actions/settings", json=body, headers=employee).status_code == 403
    assert client.put("/actions/settings", json=body, headers=hr).json()["change_approval"] is False
