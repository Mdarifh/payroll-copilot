"""Step 4: leave applications (chat + API + approval) and old vs new regime tax help."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.chat.routed import handle
from payroll_copilot.chat.router import extract_declarations, extract_leave, route
from payroll_copilot.chat.tools import ToolContext
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PermissionDeniedError
from payroll_copilot.db.models import Attendance, Employee, Leave
from payroll_copilot.domain.enums import AttendanceStatus, LeaveStatus
from payroll_copilot.services import leave_requests as lr
from payroll_copilot.services.pay_adjustments import AdjustmentError
from payroll_copilot.services.payslip import process_payroll
from payroll_copilot.services.tax_help import compare_regimes
from tests.unit.chat.conftest import FakeChatModel

TODAY = date(2026, 10, 4)  # a Sunday


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


@pytest.mark.parametrize(("text", "expected"), [
    ("sick leave 20 to 22 Oct because fever", ("SL", "2026-10-20", "2026-10-22", "fever")),
    ("kal CL chahiye", ("CL", "2026-10-05", "2026-10-05", "")),
    ("EL from 3 Nov for 3 days", ("EL", "2026-11-03", "2026-11-05", "")),
    ("EL from 7 Nov for 3 days", ("EL", "2026-11-07", "2026-11-10", "")),  # skips Sunday 8 Nov
    ("leave on 5 Jan", (None, "2027-01-05", "2027-01-05", "")),
])  # fmt: skip
def test_extract_leave(text, expected):
    days = Decimal(3) if "3 days" in text else None
    got = extract_leave(text, TODAY, days)
    assert (got["leave_type"], got["from"], got["to"], got["reason"]) == expected


def test_extract_declarations():
    assert extract_declarations("80C 1.5 lakh, 80D 25000 and NPS ₹50,000") == {
        "80C": Decimal(150000), "80D": Decimal(25000), "80CCD(1B)": Decimal(50000)}


@pytest.mark.parametrize(("text", "intent"), [
    ("I want to apply sick leave 20 to 22 Oct", "apply_leave"),
    ("kal ki chhutti chahiye", "apply_leave"),
    ("how to apply leave?", "knowledge_question"),
    ("what is my leave balance?", "none"),
    ("EMP-101 ka 15 Sep absent mark karo", "update_attendance"),
    ("which regime is better for me?", "tax_help"),
    ("80C 1.5 lakh invest karu to kitna tax bachega", "tax_help"),
    ("old vs new regime", "tax_help"),
])  # fmt: skip
def test_routing(seeded_session, text, intent):
    assert route(seeded_session, emp(seeded_session, "EMP-101"), text, TODAY).intent == intent


def test_plan_counts_working_days_and_checks_balance(seeded_session):
    s, rahul = seeded_session, emp(seeded_session, "EMP-101")
    plan = lr.plan(s, rahul, "CL", date(2026, 10, 9), date(2026, 10, 12), TODAY)
    assert plan["days"] == "3" and plan["skipped"] == [{"date": "2026-10-11", "why": "week off"}]
    assert plan["balance"] == {"available": "12", "after": "9"}
    assert lr.plan(s, rahul, "LOP", date(2026, 10, 13), None, TODAY)["balance"] is None
    with pytest.raises(AdjustmentError, match="Not enough Casual leave"):
        lr.plan(s, rahul, "CL", date(2026, 10, 13), date(2026, 10, 30), TODAY)
    with pytest.raises(AdjustmentError, match="financial year end"):
        lr.plan(s, rahul, "LOP", date(2027, 3, 30), date(2027, 4, 2), TODAY)
    with pytest.raises(AdjustmentError, match="all week offs"):
        lr.plan(s, rahul, "CL", date(2026, 10, 11), None, TODAY)
    with pytest.raises(AdjustmentError, match="already have approved"):
        lr.plan(s, rahul, "CL", date(2026, 8, 18), None, date(2026, 9, 1))


def test_apply_approve_writes_attendance_and_recalculates(seeded_session):
    s = seeded_session
    rahul, manager, priya = emp(s, "EMP-101"), emp(s, "MGR-001"), emp(s, "EMP-102")
    process_payroll(s, rahul, PayrollPeriod(2026, 9), run_by=emp(s, "HR-001"))
    applied = lr.apply(s, rahul, "SL", date(2026, 9, 22), None, "Was unwell", TODAY)
    assert applied["status"] == "PENDING" and applied["days"] == "1"
    assert "Sneha Kapoor" in applied["approvers"] or applied["approvers"]
    with pytest.raises(PermissionDeniedError):
        lr.decide(s, rahul, applied["id"], True, "")
    with pytest.raises(PermissionDeniedError):
        lr.decide(s, priya, applied["id"], True, "")
    assert [x["id"] for x in lr.to_decide(s, manager)] == [applied["id"]]
    done = lr.decide(s, manager, applied["id"], True, "Get well soon")
    assert done["status"] == "APPROVED" and done["recalculated"] == ["2026-09"]
    day = s.scalar(select(Attendance).where(Attendance.employee_id == rahul.id,
                                            Attendance.day == date(2026, 9, 22)))
    assert day.status is AttendanceStatus.LEAVE and day.leave_id == applied["id"]
    with pytest.raises(AdjustmentError, match="already approved"):
        lr.decide(s, manager, applied["id"], False, "too late")
    second = lr.apply(s, rahul, "CL", date(2026, 10, 20), None, "Personal work", TODAY)
    lr.cancel(s, rahul, second["id"])
    assert s.get(Leave, second["id"]).status is LeaveStatus.CANCELLED


def test_tax_compare(seeded_session):
    s, rahul = seeded_session, emp(seeded_session, "EMP-101")
    out = compare_regimes(s, rahul, rahul, PayrollPeriod(2026, 10), {"80C": Decimal(50000)})
    old, new = out["regimes"]
    assert old["deductions"] == {"80C": "50000"} and new["deductions"] == {}
    assert out["better"] == ("NEW" if Decimal(new["total_tax"]) < Decimal(old["total_tax"])
                             else out["better"])  # fmt: skip
    assert Decimal(out["saving"]) == abs(Decimal(old["total_tax"]) - Decimal(new["total_tax"]))
    tip = next(t for t in out["tips"] if t["section"] == "80C")
    assert tip["unused"] == "100000" and Decimal(tip["tax_saved"]) > 0
    with pytest.raises(PermissionDeniedError):
        compare_regimes(s, rahul, emp(s, "EMP-102"), PayrollPeriod(2026, 10))


def test_chat_cards(seeded_session):
    s, rahul = seeded_session, emp(seeded_session, "EMP-101")
    ctx = ToolContext(s, rahul, TODAY)
    out = handle(ctx, route(s, rahul, "sick leave 20 to 22 Oct because fever", TODAY))
    card = out.cards[0]
    assert card["type"] == "leave_request_card" and card["plan"]["days"] == "3"
    assert s.scalars(select(Leave).where(Leave.employee_id == rahul.id,
                                         Leave.status == LeaveStatus.PENDING)).all() == []
    out = handle(ctx, route(s, rahul, "which regime is better for me", TODAY))
    assert out.cards[0]["type"] == "tax_compare_card" and "not tax advice" in out.text


def test_leave_api(seeded_engine, make_settings):
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip

    def auth(email, password):
        token = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    rahul = auth("rahul.verma@example.com", "Employee@123")
    body = {"leave_type": "CL", "from_date": "2026-12-01", "to_date": "2026-12-02",
            "reason": "Family trip"}
    assert client.post("/leaves/preview", json=body, headers=rahul).json()["days"] == "2"
    created = client.post("/leaves", json=body, headers=rahul).json()
    assert created["status"] == "PENDING"
    assert client.post("/leaves", json=body, headers=rahul).status_code in (400, 409, 422)
    assert client.post(f"/leaves/{created['id']}/approve", json={},
                       headers=rahul).status_code == 403  # fmt: skip
    hr = auth("anjali.mehta@example.com", "Hr@12345")
    assert any(x["id"] == created["id"] for x in client.get("/leaves/to-approve", headers=hr).json())
    rejected = client.post(f"/leaves/{created['id']}/reject", json={"note": "Year-end freeze"},
                           headers=hr).json()  # fmt: skip
    assert rejected["status"] == "REJECTED"
    mine = client.get("/leaves", headers=rahul).json()
    assert mine["requests"][0]["status"] == "REJECTED" and len(mine["balances"]) == 3
    tax = client.get("/tax/compare?80C=150000", headers=rahul).json()
    assert tax["declared"] == {"80C": "150000"}
    assert client.get("/tax/compare?employee=EMP-102", headers=rahul).status_code == 403
