"""Employee Directory: role-scoped list, filters, salary tabs for a month."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.core.exceptions import PermissionDeniedError
from payroll_copilot.db.models import Employee
from payroll_copilot.services import employee_directory as svc
from tests.unit.chat.conftest import FakeChatModel

SEP = PayrollPeriod(2026, 9)


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def test_hr_sees_everyone_with_search_and_filters(seeded_session):
    hr = emp(seeded_session, "HR-001")
    everyone = svc.list_employees(seeded_session, hr, page_size=100)
    assert everyone["total"] == len(everyone["items"]) > 3
    page = svc.list_employees(seeded_session, hr, page=1, page_size=2)
    assert len(page["items"]) == 2 and page["pages"] == -(-everyone["total"] // 2)
    found = svc.list_employees(seeded_session, hr, search="emp-101")["items"]
    assert [i["employee_code"] for i in found] == ["EMP-101"]
    low = svc.list_employees(seeded_session, hr, structure="LOW-2026", page_size=100)["items"]
    assert low and {i["structure_code"] for i in low} == {"LOW-2026"}
    f = svc.filters(seeded_session, hr)
    assert "Engineering" in f["departments"] and {"STD-2026", "LOW-2026"} <= {
        x["code"] for x in f["structures"]}


def test_employee_sees_only_self(seeded_session):
    me = emp(seeded_session, "EMP-101")
    assert [i["employee_code"] for i in svc.list_employees(seeded_session, me)["items"]] == [
        "EMP-101"]
    with pytest.raises(PermissionDeniedError):
        svc.salary_details(seeded_session, me, "EMP-102", SEP)


def test_manager_sees_team_profiles_without_amounts(seeded_session):
    mgr = emp(seeded_session, "MGR-001")
    team = svc.list_employees(seeded_session, mgr, page_size=100)["items"]
    other = next(i for i in team if i["employee_code"] != "MGR-001")
    detail = svc.salary_details(seeded_session, mgr, other["employee_code"], SEP)
    assert detail["restricted"] and "earnings" not in detail and "monthly_gross" not in detail


def test_salary_tabs_come_from_the_structure(seeded_session):
    hr = emp(seeded_session, "HR-001")
    d = svc.salary_details(seeded_session, hr, "EMP-101", SEP)
    assert not d["restricted"]
    assert Decimal(d["annual_gross"]) == Decimal(d["monthly_gross"]) * 12
    heads = {r["head_code"]: r for r in d["earnings"]["rows"]}
    assert {"BASIC", "HRA", "SPECIAL"} <= heads.keys()
    if d["structure"]["code"] == "STD-2026":
        assert heads["BASIC"]["basis"] == "50% of gross"
        assert heads["HRA"]["basis"] == "50% of Basic"
        assert heads["SPECIAL"]["basis"] == "Balance of gross"
    assert Decimal(d["earnings"]["total"]) == sum(
        Decimal(r["monthly_amount"]) for r in d["earnings"]["rows"])
    allowances = {r["head_code"]: r for r in d["allowances"]["rows"]}
    assert {"CONVEYANCE", "MEAL", "SHIFT", "NIGHT", "TELEPHONE", "FUEL"} <= allowances.keys()
    assert allowances["TELEPHONE"]["tax_treatment"] == "Exempt on bills"
    assert allowances["MEAL"]["tax_treatment"] == "Exempt with limit"
    total = Decimal(d["earnings"]["total"]) + Decimal(d["allowances"]["total"])
    assert total == Decimal(d["monthly_gross"])  # every gross head is shown in one tab
    assert d["net_pay"]["amount"] is not None


def test_structure_detail_and_http(seeded_engine, make_settings):
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip
    token = client.post("/auth/login", json={"email": "anjali.mehta@example.com",
                                             "password": "Hr@12345"}).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/employees?page_size=5", headers=h).json()["items"]
    r = client.get("/employees/EMP-101/salary?month=2026-09", headers=h)
    assert r.status_code == 200 and r.json()["month"] == "2026-09"
    s = client.get("/structures/STD-2026", headers=h).json()
    assert s["components"][0]["head_code"] == "BASIC" and s["employees"] >= 1
    assert client.get("/structures/NOPE", headers=h).status_code == 404
    assert client.get("/employees").status_code == 401
