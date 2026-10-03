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
    ("Run September 2026 payroll", "run_payroll", None, {}),
    ("Run AI payroll audit for September 2026", "payroll_audit", None, {}),
    ("How is my PF calculated?", "payroll_explanation", None, {"component": "PF"}),
    ("Add 4 hours overtime for EMP-101", "add_overtime", "EMP-101", {"hours": Decimal(4)}),
    ("Priya Nair details", "employee_lookup", "EMP-102", {}),
    ("Show my latest payslip", "none", None, {}),  # stays with the LLM tools
    ("What is my leave balance?", "none", None, {}),
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


def test_unrouted_messages_still_use_the_llm(seeded_session):
    from tests.unit.chat.conftest import say

    model = FakeChatModel(say("Hello!"))
    reply = ChatAgent(model).ask(_ctx(seeded_session, "EMP-101"), "hello", "c")
    assert reply.messages[0]["text"] == "Hello!" and model.calls
