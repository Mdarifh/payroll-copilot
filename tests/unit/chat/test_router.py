"""Intent router (Phase 5): intent, employee, months and numbers from English / Hinglish,
and routed answers that come from the payroll engine - writes only ever preview."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from payroll_copilot.chat.agent import ChatAgent
from payroll_copilot.chat.router import extract_amount, extract_months, route
from payroll_copilot.core.dates import PayrollPeriod
from payroll_copilot.db.models import Employee, LopRecord, OvertimeEntry, PayAdjustment
from tests.unit.chat.conftest import FakeChatModel

TODAY = date(2026, 10, 3)


def emp(s, code) -> Employee:
    return s.scalar(select(Employee).where(Employee.employee_code == code))


@pytest.mark.parametrize(("message", "intent", "employee", "extra"), [
    ("Rahul ka September PF kitna hai?", "payroll_explanation", "EMP-101", {"component": "PF"}),
    ("Rahul ki salary October mein kyun change hui?", "payroll_comparison", "EMP-101", {}),
    ("September payroll mein PF mismatch find karo.", "payroll_audit", None, {}),
    ("Rahul ka LOP 4 days kar do.", "update_lop", "EMP-101", {"days": Decimal(4)}),
    ("Rahul ko 5 hours overtime do.", "add_overtime", "EMP-101", {"hours": Decimal(5)}),
    ("Rahul ke payroll mein ₹2500 miscellaneous earning add karo", "add_miscellaneous",
     "EMP-101", {"amount": Decimal(2500)}),
    ("EMP-102 ko 5000 bonus do", "add_bonus", "EMP-102", {"amount": Decimal(5000)}),
    ("Explain PF", "compliance_question", None, {"component": "PF"}),
    ("Compare Rahul August vs September", "payroll_comparison", "EMP-101", {}),
    ("Run September 2026 payroll", "run_payroll", None, {"extra": {}}),
    ("EMP-101 ka October payroll process karo", "run_payroll", "EMP-101",
     {"extra": {"employees": ["EMP-101"]}}),
    ("Run payroll for EMP-101 and EMP-102", "run_payroll", "EMP-101",
     {"extra": {"employees": ["EMP-101", "EMP-102"]}}),
    ("Run AI payroll audit for September 2026", "payroll_audit", None, {}),
    ("How is my PF calculated?", "payroll_explanation", None, {"component": "PF"}),
    ("Add 4 hours overtime for EMP-101", "add_overtime", "EMP-101", {"hours": Decimal(4)}),
    ("Priya Nair details", "employee_lookup", "EMP-102", {}),
    ("Show my latest payslip", "payslip_lookup", None, {}),  # own: fast lane, no LLM
    ("meri september ki payslip dikhao", "payslip_lookup", None, {}),
    ("How do I download my payslip?", "none", None, {}),
    ("What is my leave balance?", "leave_balance", None, {}),
    ("meri kitni chhutti bachi hai", "leave_balance", None, {}),
    ("Show my tax summary", "tax_summary", None, {}),
    ("meri leave cancel karo", "my_leave_requests", None, {"flags": {"cancel"}}),
    ("my leave status", "my_leave_requests", None, {}),
    ("pending leaves dikhao", "leave_approvals", None, {}),
    ("Rahul ki leave approve karo", "leave_approvals", "EMP-101", {}),
    ("leave approval process kya hai", "knowledge_question", None, {}),
    ("leave request for 20 Oct", "apply_leave", None, {}),
    ("Show pending approvals", "pending_actions", None, {}),
])  # fmt: skip
def test_route(seeded_session, message, intent, employee, extra):
    r = route(seeded_session, emp(seeded_session, "HR-001"), message, TODAY)
    assert (r.intent, r.employee) == (intent, employee)
    for key, value in extra.items():
        assert getattr(r, key) == value


def test_months_and_amounts():
    assert extract_months("September", TODAY) == [PayrollPeriod(2026, 9)]
    assert extract_months("December", TODAY) == [PayrollPeriod(2025, 12)]  # latest past one
    assert extract_months("Aug vs Sep 2026", TODAY) == [PayrollPeriod(2026, 8),
                                                        PayrollPeriod(2026, 9)]
    assert extract_months("last month", TODAY) == [PayrollPeriod(2026, 9)]
    assert extract_months("you may go", TODAY) == []  # the verb, not the month
    for written in ("run payroll of nov-2026", "Nov, 2026", "nov-26", "nov'26", "nov/2026"):
        assert extract_months(written, TODAY) == [PayrollPeriod(2026, 11)], written
    assert extract_months("sep 15 absent", TODAY) == [PayrollPeriod(2026, 9)]  # 15 is a day
    assert extract_amount("₹2,500") == 2500 and extract_amount("2.5k") == 2500
    assert extract_amount("9 lakh") == 900000 and extract_amount("in 2026") is None


def test_ambiguous_name_asks_which_one(seeded_session):
    s = seeded_session
    first = s.scalar(select(Employee).order_by(Employee.id))
    twin = Employee(**{c.name: getattr(first, c.name) for c in Employee.__table__.columns
                       if c.name not in ("id", "employee_code", "email", "full_name")},
                    employee_code="EMP-999", email="rahul.k@example.com", full_name="Rahul Kumar")
    s.add(twin)
    s.flush()
    r = route(s, emp(s, "HR-001"), "Rahul ka PF kitna hai", TODAY)
    assert r.employee is None and "Which one" in r.employee_error


def _ask(s, code, message):
    return ChatAgent(FakeChatModel()).ask(_ctx(s, code), message, "conv")


def _ctx(s, code):
    from payroll_copilot.chat.tools import ToolContext

    return ToolContext(session=s, user=emp(s, code), today=TODAY)


def test_explanation_comes_from_the_engine(seeded_session):
    reply = _ask(seeded_session, "HR-001", "Rahul ka September PF kitna hai?")
    card = reply.messages[1]
    assert card["type"] == "explain_card" and card["component"] == "PF"
    assert f"₹{int(Decimal(card['amount'])):,}" in reply.messages[0]["text"]
    assert reply.model == "intent-router" and reply.tools_used[0] == "router:payroll_explanation"


def test_write_intent_only_previews(seeded_session):
    s = seeded_session

    def rows():
        return tuple(s.scalar(select(func.count()).select_from(m))
                     for m in (LopRecord, OvertimeEntry, PayAdjustment))

    before = rows()
    reply = _ask(s, "HR-001", "Rahul ka LOP 2 days kar do")
    card = reply.messages[1]
    assert card["type"] == "action_preview" and card["saved"] is False
    assert card["proposal"]["change"]["lop_days"] == "2"
    assert "Nothing has been changed" in reply.messages[0]["text"]
    assert rows() == before


def test_employee_cannot_preview_or_see_others(seeded_session):
    s = seeded_session
    assert "Only HR" in _ask(s, "EMP-101", "Rahul ka LOP 2 days kar do").messages[0]["text"]
    reply = _ask(s, "EMP-101", "Priya ka September PF kitna hai")
    assert "own payroll" in reply.messages[0]["text"] and len(reply.messages) == 1


def test_own_payslip_leave_and_tax_skip_the_llm(seeded_session):
    s = seeded_session
    reply = _ask(s, "EMP-101", "meri payslip dikhao")
    assert reply.model == "intent-router" and reply.messages[1]["type"] == "payslip_card"
    assert reply.messages[0]["text"].startswith("Your ")
    assert reply.messages[1]["download_url"]  # own payslip: the PDF link is there
    reply = _ask(s, "EMP-101", "What is my leave balance?")
    assert reply.model == "intent-router" and "Your leave balance" in reply.messages[0]["text"]
    assert reply.messages[1]["type"] == "info_card" and reply.messages[1]["fields"]
    reply = _ask(s, "EMP-101", "Show my tax summary")
    assert reply.model == "intent-router" and reply.tools_used[0] == "router:tax_summary"


def test_leave_requests_can_be_decided_from_chat(seeded_session):
    from payroll_copilot.services import leave_requests

    s = seeded_session
    leave = leave_requests.apply(s, emp(s, "EMP-101"), "CL", date(2026, 10, 20), None,
                                 "Family function", today=TODAY)  # fmt: skip
    mine = _ask(s, "EMP-101", "my leave status")
    card = mine.messages[1]
    assert card["type"] == "leave_list_card" and card["mode"] == "mine"
    assert card["items"][0]["id"] == leave["id"] and card["items"][0]["status"] == "PENDING"
    assert "withdrawn" in mine.messages[0]["text"]
    hr = _ask(s, "HR-001", "pending leaves dikhao")
    card = hr.messages[1]
    assert card["type"] == "leave_list_card" and card["mode"] == "approve"
    assert leave["id"] in [i["id"] for i in card["items"]]
    rahul = _ask(s, "HR-001", "Rahul ki leave approve karo").messages[1]["items"]
    assert {i["employee_code"] for i in rahul} == {"EMP-101"}  # only that employee's requests
    # nobody decides their own leave
    own = _ask(s, "EMP-101", "pending leaves dikhao")
    assert own.messages[0]["text"].startswith("No leave requests") and len(own.messages) == 1


def test_pending_payroll_changes_card(seeded_session):
    from payroll_copilot.services import action_center
    from payroll_copilot.services.simulation import PayrollChange, simulate

    s = seeded_session
    assert "No payroll changes" in _ask(s, "HR-001", "Show pending approvals").messages[0]["text"]
    change = PayrollChange(lop_days=Decimal(2), reason="Absent 14th and 15th, confirmed")
    pid = simulate(s, emp(s, "HR-001"), "EMP-101", PayrollPeriod(2026, 9), change)["preview_id"]
    sent = action_center.submit(s, emp(s, "HR-001"), "EMP-101", PayrollPeriod(2026, 9), change,
                                pid)  # fmt: skip
    assert sent["status"] == "PENDING_APPROVAL"
    maker = _ask(s, "HR-001", "Show pending approvals")
    card = maker.messages[1]
    assert card["type"] == "action_list_card" and card["items"][0]["id"] == sent["id"]
    assert card["me"] == "HR-001" and "another HR user must approve" in maker.messages[0]["text"]
    checker = _ask(s, "HR-002", "Show pending approvals")
    assert "yours" not in checker.messages[0]["text"] and checker.messages[1]["can_approve"]
    assert "Only HR" in _ask(s, "EMP-101", "Show pending approvals").messages[0]["text"]


def test_unrouted_messages_still_use_the_llm(seeded_session):
    from tests.unit.chat.conftest import say

    model = FakeChatModel(say("Hello!"))
    reply = ChatAgent(model).ask(_ctx(seeded_session, "EMP-101"), "hello", "c")
    assert reply.messages[0]["text"] == "Hello!" and model.calls
