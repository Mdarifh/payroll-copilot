import pytest

from payroll_copilot.agent.actions import parse_action
from payroll_copilot.agent.intents import Intent, detect_intent
from payroll_copilot.domain.enums import ActionType

ENTITIES = {"employee_code": "EMP-009", "period": "2026-09"}


@pytest.mark.parametrize(("question", "intent"), [
    ("EMP-009 ka TDS 2026-09 ke liye 3000 override karo", Intent.ACTION_TDS_OVERRIDE),
    ("override TDS for EMP-009 to 3000", Intent.ACTION_TDS_OVERRIDE),
    ("correct LOP of EMP-002 for 2026-09 to 2", Intent.ACTION_ATTENDANCE),
    ("EMP-002 ka LOP 2026-09 mein 2 kar do", Intent.ACTION_ATTENDANCE),
    ("save QA report for 2026-09", Intent.ACTION_QA_SAVE),
    ("verify rule EPF v2 as per notification", Intent.ACTION_RULE_VERIFY),
    ("EMP-002 ke LOP kitne hain", Intent.ATTENDANCE),  # a question, not an action
    ("EMP-009 ka TDS kitna hai", Intent.TDS),
])  # fmt: skip
def test_action_intents(question, intent):
    assert detect_intent(question, True)[0] is intent


@pytest.mark.parametrize(("intent", "question", "expected"), [
    (Intent.ACTION_TDS_OVERRIDE, "EMP-009 ka TDS 2026-09 ke liye 3,000 override karo",
     {"amount": "3000", "reason": "requested via chat by cli:x"}),
    (Intent.ACTION_TDS_OVERRIDE, "override TDS for EMP-009 to ₹2500 because employee request",
     {"amount": "2500", "reason": "employee request"}),
    (Intent.ACTION_ATTENDANCE, "EMP-002 ka LOP 2026-09 mein 2 kar do", {"lop_days": "2"}),
    (Intent.ACTION_ATTENDANCE, "correct LOP of EMP-002 for 2026-09 to 1.5", {"lop_days": "1.5"}),
])  # fmt: skip
def test_parse_values(intent, question, expected):
    _, payload, problem = parse_action(intent, question, ENTITIES, "cli:x")
    assert problem is None
    assert {k: payload[k] for k in expected} == expected


def test_parse_rule_and_qa():
    action, payload, _ = parse_action(Intent.ACTION_RULE_VERIFY,
                                      "verify rule EPF v2 as per EPFO circular 12", {}, "cli:x")
    assert action is ActionType.VERIFY_RULE
    assert payload == {"rule_code": "EPF", "version": 2, "evidence": "EPFO circular 12"}
    _, payload, _ = parse_action(Intent.ACTION_QA_SAVE, "save QA", ENTITIES, "cli:x")
    assert payload == {"period": "2026-09"}


@pytest.mark.parametrize(("intent", "question", "needle"), [
    (Intent.ACTION_TDS_OVERRIDE, "override TDS for EMP-009 in 2026-09", "kitna TDS"),
    (Intent.ACTION_RULE_VERIFY, "verify rule please", "kaunsa rule"),
    (Intent.ACTION_RULE_VERIFY, "verify rule EPF v2", "evidence"),
])  # fmt: skip
def test_parse_problems(intent, question, needle):
    _, payload, problem = parse_action(intent, question, ENTITIES, "cli:x")
    assert payload is None and needle in problem
