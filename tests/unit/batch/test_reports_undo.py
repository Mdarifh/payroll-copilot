"""Phase 11: reports (JSON / CSV / Excel / PDF) and undo of applied changes."""

from decimal import Decimal
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from payroll_copilot.api.main import create_app
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Employee, OvertimeEntry, PayrollRun
from payroll_copilot.repositories.attendance_repo import AttendanceRepository
from payroll_copilot.services import action_center as ac
from payroll_copilot.services import reports as rp
from payroll_copilot.services.payslip import process_payroll
from payroll_copilot.services.simulation import PayrollChange, simulate
from tests.unit.chat.conftest import FakeChatModel

SEP = PayrollPeriod(2026, 9)


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def test_every_report_in_every_format(seeded_session):
    from openpyxl import load_workbook

    s = seeded_session
    for code in ("EMP-101", "EMP-102", "EMP-103"):
        process_payroll(s, emp(s, code), SEP, run_by=emp(s, "HR-001"))
    runs = s.scalars(select(PayrollRun).where(PayrollRun.period == "2026-09")).all()
    register = rp.build(s, "payroll_register", SEP)
    assert register.totals["net"] == sum(r.net_pay for r in runs)
    assert len(register.rows) == len(runs)
    for key in rp.REPORTS:
        built = rp.build(s, key, SEP)
        csv_bytes, _, name = rp.export(built, "csv")
        assert name == f"{key}_2026-09.csv" and csv_bytes.startswith(b"\xef\xbb\xbf")
        xlsx, _, _ = rp.export(built, "xlsx")
        sheet = load_workbook(BytesIO(xlsx)).active
        assert sheet["A1"].value.startswith(built.title)
        pdf, media, _ = rp.export(built, "pdf")
        assert pdf.startswith(b"%PDF") and media == "application/pdf"
    with pytest.raises(Exception, match="Unknown report"):
        rp.build(s, "nope", SEP)


def submit(s, actor, change):
    return ac.submit(s, actor, "EMP-101", SEP, change,
                     simulate(s, actor, "EMP-101", SEP, change)["preview_id"])


def test_undo_restores_lop_and_cancels_added_entries(seeded_session):
    s = seeded_session
    hr = emp(s, "HR-001")
    ac.set_approval(s, hr, False, "Single HR team for the test")
    process_payroll(s, emp(s, "EMP-101"), SEP, run_by=hr)
    base_net = s.scalar(select(PayrollRun.net_pay).where(PayrollRun.period == "2026-09"))
    done = submit(s, hr, PayrollChange(lop_days=Decimal(2),
                                       overtime=[{"work_date": "2026-09-12", "hours": "3"}],
                                       reason="LOP and weekend overtime"))  # fmt: skip
    assert done["status"] == "APPLIED" and done["result"]["undo"]["lop_days"] == "0"
    preview = ac.undo_preview(s, hr, done["id"])
    undone = ac.undo(s, hr, done["id"], preview["preview_id"], "Entered for the wrong person")
    assert undone["status"] == "APPLIED" and undone["source"] == "undo"
    assert AttendanceRepository(s).lop_record(emp(s, "EMP-101").id, SEP).days == 0
    assert {o.status for o in s.scalars(select(OvertimeEntry))} == {"CANCELLED"}
    net = s.scalar(select(PayrollRun.net_pay).where(PayrollRun.period == "2026-09"))
    assert net == base_net  # back to the original payslip
    with pytest.raises(ac.ActionConflictError, match="Already undone"):
        ac.undo_preview(s, hr, done["id"])
    revision = submit(s, hr, PayrollChange(
        salary={"monthly_gross": "65000", "effective_from": "2026-09-01"}, reason="Annual hike"))
    with pytest.raises(ac.ActionConflictError, match="salary revision cannot be undone"):
        ac.undo_preview(s, hr, revision["id"])


def test_reports_over_http(seeded_engine, make_settings):
    client = TestClient(create_app(make_settings(), engine=seeded_engine,
                                   chat_model=FakeChatModel(), worker=None))  # fmt: skip

    def auth(email, password):
        token = client.post("/auth/login", json={"email": email, "password": password})
        return {"Authorization": f"Bearer {token.json()['access_token']}"}

    viewer = auth("neha.gupta@example.com", "Hr@12345")
    employee = auth("rahul.verma@example.com", "Employee@123")
    assert client.get("/reports", headers=employee).status_code == 403
    keys = [r["key"] for r in client.get("/reports", headers=viewer).json()]
    assert "payroll_register" in keys and "anomaly" in keys
    page = client.get("/reports/pf?month=2026-08", headers=viewer).json()
    assert page["columns"][0]["key"] == "employee_code"
    excel = client.get("/reports/payroll_register?month=2026-08&format=xlsx", headers=viewer)
    assert excel.status_code == 200 and "payroll_register_2026-08.xlsx" in excel.headers[
        "content-disposition"]
