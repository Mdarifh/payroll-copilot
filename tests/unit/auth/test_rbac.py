"""Phase 10: roles and permissions, users and roles, audit timeline."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.auth.permissions import Perm, has, is_staff
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PayrollCopilotError, PermissionDeniedError
from payroll_copilot.db.models import Employee
from payroll_copilot.domain.enums import Role
from payroll_copilot.services import action_center as ac
from payroll_copilot.services import audit_timeline, users
from payroll_copilot.services.simulation import PayrollChange, simulate
from tests.unit.chat.conftest import FakeChatModel

SEP = PayrollPeriod(2026, 9)
PW = "Hr@12345"
STAFF = {"admin": "anjali.mehta@example.com", "manager": "kavita.rao@example.com",
         "executive": "sameer.khan@example.com", "viewer": "neha.gupta@example.com"}


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def test_permission_map():
    assert has(Role.VIEWER, Perm.VIEW_PAYROLL) and not has(Role.VIEWER, Perm.EDIT_ATTENDANCE)
    assert has(Role.PAYROLL_EXECUTIVE, Perm.EDIT_ATTENDANCE)
    assert not has(Role.PAYROLL_EXECUTIVE, Perm.EDIT_PAY)
    assert has(Role.PAYROLL_MANAGER, Perm.APPROVE) and not has(Role.PAYROLL_MANAGER, Perm.ADMIN)
    assert has(Role.HR, Perm.ADMIN) and has(Role.ADMIN, Perm.ADMIN)
    assert not is_staff(Role.EMPLOYEE) and not is_staff(Role.MANAGER)


def test_change_requests_follow_the_role(seeded_session):
    s = seeded_session
    lop = PayrollChange(lop_days=Decimal(1), reason="Absent on the 15th")
    bonus = PayrollChange(adjustments=[{"kind": "EARNING", "head_code": "BONUS", "amount": "500"}],
                          reason="Spot award for release")  # fmt: skip

    def submit(code, change):
        actor = emp(s, code)
        return ac.submit(s, actor, "EMP-101", SEP, change,
                         simulate(s, actor, "EMP-101", SEP, change)["preview_id"])

    assert submit("PX-001", lop)["status"] == "PENDING_APPROVAL"  # executive: LOP ok
    with pytest.raises(PermissionDeniedError):
        submit("PX-001", bonus)  # ... but not pay
    with pytest.raises(PermissionDeniedError):
        submit("VW-001", lop)  # viewer: read only
    pending = submit("PM-001", bonus)
    with pytest.raises(PermissionDeniedError):
        ac.reject(s, emp(s, "PX-001"), pending["id"], "executive cannot reject")
    approvers = [a["name"] for a in ac.approval_setting(s, emp(s, "PM-001"))["approvers"]]
    assert "Kavita Rao" not in approvers and "Rohit Kapoor" in approvers


def test_users_and_roles_guards(seeded_session):
    s = seeded_session
    admin = emp(s, "HR-001")
    out = users.update_user(s, admin, "VW-001", "payroll_executive", None, "Promoted to executive")
    assert out["role"] == "payroll_executive"
    with pytest.raises(PermissionDeniedError, match="your own"):
        users.update_user(s, admin, "HR-001", "viewer", None, "self demotion")
    users.update_user(s, admin, "HR-002", "viewer", None, "Rohit moved to finance")
    with pytest.raises(PayrollCopilotError, match="one active Admin"):
        users.update_user(s, emp(s, "HR-002"), "HR-001", None, False, "lock out the last admin")
    listed = users.list_users(s)
    assert {r["role"] for r in listed["roles"]} >= {"admin", "viewer", "payroll_manager"}
    events = audit_timeline.timeline(s, employee_code="VW-001")["items"]
    assert events[0]["label"] == "User role / login changed"
    assert "payroll_executive" in events[0]["summary"]


def test_endpoints_enforce_roles(seeded_engine, make_settings):
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip

    def auth(who, password=PW):
        token = client.post("/auth/login", json={"email": STAFF.get(who, who), "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    viewer, executive, manager = auth("viewer"), auth("executive"), auth("manager")
    admin = auth("admin")
    employee = auth("rahul.verma@example.com", "Employee@123")
    assert client.get("/payroll-runs", headers=viewer).status_code == 200  # read
    assert client.post("/batches", json={"month": "2026-09"}, headers=viewer).status_code == 403
    assert client.post("/batches", json={"month": "2026-09"}, headers=executive).status_code == 403
    assert client.post("/batches", json={"month": "2026-09"}, headers=manager).status_code == 201
    assert client.get("/audit/timeline", headers=viewer).status_code == 403
    assert client.get("/audit/timeline", headers=executive).status_code == 200
    assert client.get("/settings/users", headers=manager).status_code == 403
    assert client.get("/settings/users", headers=admin).status_code == 200
    assert client.get("/payroll-runs", headers=employee).status_code == 403
    page = client.get("/audit/timeline?action=batch", headers=admin).json()
    assert page["items"][0]["label"] == "Payroll review started"
    assert client.get("/settings/company", headers=viewer).json()["overtime_policy"]["version"]

