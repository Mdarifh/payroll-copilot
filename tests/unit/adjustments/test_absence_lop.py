"""Absences and LOP stay in step: chat defaults, payroll review, upcoming salary revisions."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from payroll_copilot.chat.router import route
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Employee, LopRecord, SalaryStructure
from payroll_copilot.services import batch_service
from payroll_copilot.services import employee_directory as directory
from payroll_copilot.services.payroll_runs import prechecks

SEP = PayrollPeriod(2026, 9)
TODAY = date(2026, 10, 3)


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


def test_chat_marks_an_absence_with_lop_unless_told_otherwise(seeded_session):
    hr = emp(seeded_session, "HR-001")
    marked = route(seeded_session, hr, "EMP-101 15 Sep absent mark karo", TODAY)
    assert marked.intent == "update_attendance" and "with_lop" in marked.flags
    for text in ("EMP-101 15 Sep absent mark karo without LOP", "EMP-101 15 Sep absent, LOP mat"):
        assert "with_lop" not in route(seeded_session, hr, text, TODAY).flags, text


def lop(s, code, days):
    s.add(LopRecord(employee_id=emp(s, code).id, period=str(SEP), days=Decimal(days),
                    reason="decided earlier", updated_by_id=emp(s, "HR-001").id))  # fmt: skip
    s.flush()


def review_codes(s) -> set[str]:
    batch = batch_service.start_review(s, emp(s, "HR-001"), SEP)
    return {i["emp_id"] for i in batch_service.lop_items(s, batch, page_size=200)["items"]}


def test_lop_decided_for_fewer_days_than_the_absences_goes_back_to_review(seeded_session):
    s = seeded_session
    assert "EMP-102" in review_codes(s)  # 2 absences, no decision yet
    lop(s, "EMP-102", "1")  # decided before a second absence was marked
    assert "EMP-102" in review_codes(s)
    att = next(c for c in prechecks(s, SEP)["checks"] if c["key"] == "ATTENDANCE")
    assert any(i["employee_code"] == "EMP-102" and "but LOP is 1" in i["message"]
               for i in att["issues"])  # fmt: skip


def test_lop_covering_every_absence_needs_no_review(seeded_session):
    s = seeded_session
    lop(s, "EMP-102", "2")
    assert "EMP-102" not in review_codes(s)


def test_an_older_month_shows_the_coming_salary_revision(seeded_session):
    s = seeded_session
    rahul = emp(s, "EMP-101")
    current = list(s.scalars(select(SalaryStructure).where(
        SalaryStructure.employee_id == rahul.id, SalaryStructure.effective_to.is_(None))))
    for row in current:
        row.effective_to = date(2026, 9, 30)
        s.add(SalaryStructure(employee_id=rahul.id, effective_from=date(2026, 10, 1),
                              head_code=row.head_code, monthly_amount=row.monthly_amount * 2,
                              template_code=row.template_code))  # fmt: skip
    s.flush()
    hr = emp(s, "HR-001")
    sep = directory.salary_details(s, hr, "EMP-101", SEP)
    upcoming = sep["next_revision"]
    assert upcoming["effective_from"] == "2026-10-01" and upcoming["month"] == "2026-10"
    assert Decimal(upcoming["monthly_gross"]) == 2 * Decimal(sep["monthly_gross"])
    assert directory.salary_details(s, hr, "EMP-101", PayrollPeriod(2026, 10))[
        "next_revision"] is None
