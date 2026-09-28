import pytest

from payroll_copilot.agent.entities import extract_entities, extract_period
from payroll_copilot.agent.intents import Intent, detect_intent, knowledge_module

DIRECTORY = [("ARIF-112", "Arif Sharma"), ("EMP-002", "Priya Nair"), ("EMP-003", "Rohan Das"),
             ("EMP-008", "Kavita Reddy"), ("EMP-009", "Rahul Verma")]  # fmt: skip


@pytest.mark.parametrize(("question", "has_employee", "intent"), [
    ("ARIF-112 ka PF September 2026 mein kitna hai?", True, Intent.PF),
    ("aur ESIC?", False, Intent.ESIC),
    ("EMP-002 ke paid days kitne hain", True, Intent.ATTENDANCE),
    ("Priya ki salary Sep 2026 mein kyun kam hui?", True, Intent.SALARY),
    ("Why was salary reduced for EMP-002?", True, Intent.SALARY),
    ("EMP-009 ka TDS", True, Intent.TDS),
    ("income tax for EMP-009", True, Intent.TDS),
    ("EMP-008 ka professional tax", True, Intent.PT),
    ("EMP-004 arrear Sep 2026", True, Intent.ARREAR),
    ("EMP-006 ka F&F", True, Intent.FNF),
    ("full and final settlement of Anjali", True, Intent.FNF),
    ("ARIF-112 PF old vs new rule", True, Intent.PF_COMPARE),
    ("Validate September 2026 payroll", False, Intent.QA_PERIOD),
    ("payroll QA for 2026-09", False, Intent.QA_PERIOD),
    ("EMP-008 expected vs actual 2026-09", True, Intent.QA_EMPLOYEE),
    ("EMP-002 ka payslip", True, Intent.PAYSLIP),
    ("EMP-002 net pay kitna hai", True, Intent.PAYSLIP),
    ("EMP-003 salary structure", True, Intent.STRUCTURE),
    ("EMP-005 DOJ kya hai", True, Intent.EMPLOYEE),
    ("What is the PF wage ceiling?", False, Intent.KNOWLEDGE),
    ("PT slab for women in Maharashtra kya hai", False, Intent.KNOWLEDGE),
    ("how is LOP reversal paid", False, Intent.KNOWLEDGE),
    ("hello", False, Intent.GENERAL),
])  # fmt: skip
def test_intents(question, has_employee, intent):
    assert detect_intent(question, has_employee)[0] is intent


def test_employee_question_about_rules_stays_a_calculation():
    assert detect_intent("ARIF-112 ka PF ceiling kya hai 2026-09", True)[0] is Intent.PF


@pytest.mark.parametrize(("question", "module"), [
    ("PF wage ceiling", "pf"), ("ESIC threshold", "esic"), ("tax slabs", "tds"),
    ("LOP policy", "attendance"), ("arrear PF", "arrears"), ("something else", None),
])  # fmt: skip
def test_knowledge_module(question, module):
    assert knowledge_module(question) == module


@pytest.mark.parametrize(("text", "period"), [
    ("PF for 2026-09", "2026-09"), ("PF for 2026/9", "2026-09"), ("09/2026", "2026-09"),
    ("September 2026", "2026-09"), ("sept 2026", "2026-09"), ("Sep-2026", "2026-09"),
    ("aug, 2026", "2026-08"), ("Feb 2027", "2027-02"), ("no month here", None),
    ("may I ask", None), ("2026-13", None),
])  # fmt: skip
def test_period(text, period):
    assert extract_period(text) == period


class TestEntities:
    def test_code_and_period(self):
        e = extract_entities("arif-112 ka PF Sep 2026", DIRECTORY)
        assert (e.employee_code, e.employee_source, e.period) == ("ARIF-112", "code", "2026-09")

    def test_name(self):
        e = extract_entities("Priya ki salary", DIRECTORY)
        assert (e.employee_code, e.employee_source) == ("EMP-002", "name")

    def test_ambiguous_names(self):
        directory = DIRECTORY + [("EMP-099", "Priya Shah")]
        e = extract_entities("Priya ki salary", directory)
        assert e.employee_code is None and "several employees" in e.notes[0]

    def test_unknown_code(self):
        e = extract_entities("EMP-999 ka PF", DIRECTORY)
        assert e.employee_code is None and e.notes == ["Employee code EMP-999 not found"]

    def test_state(self):
        assert extract_entities("PT slab in MH", DIRECTORY, {"MH", "KA"}).state == "MH"
        assert extract_entities("PT slab", DIRECTORY, {"MH"}).state is None
