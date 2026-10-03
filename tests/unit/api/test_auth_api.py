"""Login, current user and backend role checks over HTTP."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.auth.security import issue_token
from payroll_copilot.db.models import AuditLog, Employee
from payroll_copilot.domain.enums import Role

HR = ("anjali.mehta@example.com", "Hr@12345")
EMP = ("rahul.verma@example.com", "Employee@123")


@pytest.fixture
def client(seeded_engine, make_settings):
    return TestClient(create_app(make_settings(), engine=seeded_engine))


def login(client, email, password):
    return client.post("/auth/login", json={"email": email, "password": password})


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health(client):
    assert client.get("/health").json()["database"] == "ok"


def test_login_returns_token_and_user(client):
    r = login(client, "  Rahul.Verma@Example.com ", EMP[1])  # email: case/space-insensitive
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["access_token"]
    assert body["user"]["employee_code"] == "EMP-101" and body["user"]["role"] == "employee"
    me = client.get("/auth/me", headers=auth(body["access_token"])).json()
    assert me["full_name"] == "Rahul Verma"


@pytest.mark.parametrize(("email", "password"), [
    (EMP[0], "wrong-password"), ("nobody@example.com", EMP[1]), (EMP[0], HR[1]),
], ids=["wrong-password", "unknown-email", "someone-elses-password"])  # fmt: skip
def test_failed_login_same_message(client, email, password):
    r = login(client, email, password)
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid email or password"
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_login_validation(client):
    assert client.post("/auth/login", json={"email": EMP[0]}).status_code == 422
    extra = {"email": EMP[0], "password": EMP[1], "role": "hr"}  # cannot pick your own role
    assert client.post("/auth/login", json=extra).status_code == 422


def test_swagger_form_login(client):
    r = client.post("/auth/token", data={"username": HR[0], "password": HR[1]})
    assert r.status_code == 200
    assert client.get("/auth/me", headers=auth(r.json()["access_token"])).json()["role"] == "hr"


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer garbage"}, {"Authorization": "Basic abc"},
])  # fmt: skip
def test_me_needs_a_valid_token(client, headers):
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_hr_only_endpoint(client):
    emp_token = login(client, *EMP).json()["access_token"]
    hr_token = login(client, *HR).json()["access_token"]
    denied = client.get("/hr/employees", headers=auth(emp_token))
    assert denied.status_code == 403 and "role cannot" in denied.json()["detail"]
    rows = client.get("/hr/employees", headers=auth(hr_token)).json()
    assert len(rows) == 12 and {"password_hash", "pan"}.isdisjoint(rows[0])


def test_token_role_must_match_the_database(client, seeded_engine):
    # a validly signed token that says "hr" for an employee row -> rejected
    with seeded_engine.connect() as c:
        emp_id = c.scalar(select(Employee.id).where(Employee.employee_code == "EMP-101"))
    token, _ = issue_token(emp_id, Role.HR)
    r = client.get("/hr/employees", headers=auth(token))
    assert r.status_code == 401 and "Role changed" in r.json()["detail"]


def test_disabled_login_stops_existing_tokens(client, seeded_session):
    token = login(client, *EMP).json()["access_token"]
    emp = seeded_session.scalar(select(Employee).where(Employee.email == EMP[0]))
    emp.login_enabled = False
    seeded_session.commit()
    assert client.get("/auth/me", headers=auth(token)).status_code == 401
    assert login(client, *EMP).status_code == 401


def test_logins_are_audited(client, seeded_session):
    login(client, *EMP)
    login(client, EMP[0], "bad")
    login(client, "ghost@example.com", "bad")
    seeded = select(AuditLog).where(AuditLog.action != "payroll.run")  # seed: August runs
    rows = seeded_session.scalars(seeded.order_by(AuditLog.id)).all()
    assert [r.action for r in rows] == ["auth.login", "auth.login_failed", "auth.login_failed"]
    ok, bad, ghost = rows
    assert ok.actor_role == "employee" and ok.actor_id is not None and ok.request_id
    assert bad.actor_id is None and bad.new_value == {"email": EMP[0]}
    assert ghost.entity_id is None
    assert all("Employee@123" not in str(r.new_value) for r in rows)


def test_request_id_header(client):
    r = client.get("/health", headers={"X-Request-ID": "req-test-0001"})
    assert r.headers["X-Request-ID"] == "req-test-0001"
    assert len(client.get("/health").headers["X-Request-ID"]) == 16
