"""Rule-based intent detection (English + Hinglish), deterministic and explainable."""

import re
from enum import StrEnum


class Intent(StrEnum):
    ATTENDANCE = "ATTENDANCE"
    SALARY = "SALARY"
    STRUCTURE = "STRUCTURE"
    PAYSLIP = "PAYSLIP"
    PF = "PF"
    PF_COMPARE = "PF_COMPARE"
    ESIC = "ESIC"
    PT = "PT"
    TDS = "TDS"
    ARREAR = "ARREAR"
    FNF = "FNF"
    QA_EMPLOYEE = "QA_EMPLOYEE"
    QA_PERIOD = "QA_PERIOD"
    EMPLOYEE = "EMPLOYEE"
    KNOWLEDGE = "KNOWLEDGE"
    GENERAL = "GENERAL"
    # write actions (need human approval)
    ACTION_TDS_OVERRIDE = "ACTION_TDS_OVERRIDE"
    ACTION_ATTENDANCE = "ACTION_ATTENDANCE"
    ACTION_QA_SAVE = "ACTION_QA_SAVE"
    ACTION_RULE_VERIFY = "ACTION_RULE_VERIFY"


ACTION_INTENTS = {Intent.ACTION_TDS_OVERRIDE, Intent.ACTION_ATTENDANCE, Intent.ACTION_QA_SAVE,
                  Intent.ACTION_RULE_VERIFY}  # fmt: skip


NEEDS_EMPLOYEE_AND_PERIOD = {
    Intent.ATTENDANCE, Intent.SALARY, Intent.STRUCTURE, Intent.PAYSLIP, Intent.PF,
    Intent.PF_COMPARE, Intent.ESIC, Intent.PT, Intent.TDS, Intent.ARREAR, Intent.QA_EMPLOYEE,
    Intent.ACTION_TDS_OVERRIDE, Intent.ACTION_ATTENDANCE,
}  # fmt: skip
NEEDS_EMPLOYEE_ONLY = {Intent.FNF, Intent.EMPLOYEE}
NEEDS_PERIOD_ONLY = {Intent.QA_PERIOD, Intent.ACTION_QA_SAVE}

# Knowledge-base module used when an intent needs policy/rule context.
INTENT_MODULE = {
    Intent.PF: "pf", Intent.PF_COMPARE: "pf", Intent.ESIC: "esic", Intent.PT: "pt",
    Intent.TDS: "tds", Intent.ATTENDANCE: "attendance", Intent.SALARY: "payroll",
    Intent.PAYSLIP: "payroll", Intent.STRUCTURE: "payroll", Intent.ARREAR: "arrears",
    Intent.FNF: "fnf",
}  # fmt: skip


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


_PF = _rx(r"\b(pf|epf|eps|edli|provident|admin charges?)\b")
# Checked in order; the first match wins. Write actions come first.
_RULES: list[tuple[Intent, re.Pattern[str]]] = [
    (Intent.ACTION_TDS_OVERRIDE, _rx(r"\boverride\b.*\btds\b|\btds\b.*\boverride\b")),
    (Intent.ACTION_ATTENDANCE, _rx(r"\b(correct|set|update|change|fix)\b.*\blop\b.*\bto\b|"
                                   r"\blop\b.*\b(kar\s*do|karo|set\s*karo|correct\s*karo)\b")),
    (Intent.ACTION_QA_SAVE, _rx(r"\bsave\b.*\b(qa|validation|report)\b")),
    (Intent.ACTION_RULE_VERIFY, _rx(r"\bverify\b.*\brule\b|\bmark\b.*\bverified\b")),
    (Intent.FNF, _rx(r"f\s*&\s*f|\bfnf\b|full\s*(and|&)\s*final|settlement")),
    (Intent.ARREAR, _rx(r"arrear")),
    (Intent.PF_COMPARE, _rx(r"\b(pf|epf)\b.*\b(compare|comparison|old|new|purana|naya|v1|v2)\b"
                            r"|\b(compare|old|new|purana|naya)\b.*\b(pf|epf)\b.*\brule")),
    (Intent.QA_PERIOD, _rx(r"\b(validat\w*|qa|audit|check)\b.*\bpayroll\b|"
                           r"\bpayroll\b.*\b(validat\w*|qa|audit)\b")),
    (Intent.QA_EMPLOYEE, _rx(r"mismatch|expected\s*(vs|v/s|versus|and)\s*actual|discrepanc|"
                             r"\bgalat\b|\bwrong\b|\bvalidat\w*|\bdifference\b|\bcompare\b|\bqa\b")),
    (Intent.ESIC, _rx(r"\besic?\b")),
    (Intent.PF, _PF),
    (Intent.PT, _rx(r"professional\s+tax|profession\s+tax|\bpt\b")),
    (Intent.TDS, _rx(r"\btds\b|income\s*tax|\btax\b")),
    (Intent.ATTENDANCE, _rx(r"paid\s*days|\blop\b|loss\s+of\s+pay|attendance|absent|\bleaves?\b|"
                            r"working\s+days|chutti")),
    (Intent.PAYSLIP, _rx(r"pay\s*slip|net\s*(pay|salary)|in\s*hand|take\s*home|\bctc\b|"
                         r"deductions?")),
    (Intent.STRUCTURE, _rx(r"structure|break\s*-?up|components?|wage\s+bases?")),
    (Intent.SALARY, _rx(r"salary|gross|prorat\w*|reduced|\bkam\b|earnings?|\bbasic\b|\bhra\b")),
    (Intent.EMPLOYEE, _rx(r"\bdoj\b|joining|employee\s+details|details\s+of|who\s+is|\bkaun\b|"
                          r"\blwd\b|designation|department")),
]  # fmt: skip

KNOWLEDGE_WORDS = _rx(
    r"\brules?\b|policy|what\s+is|what\s+are|kya\s+hai|ceiling|slabs?|threshold|\brates?\b|"
    r"\bhow\s+(is|are|does|do)\b|explain|meaning"
)
WHY_WORDS = _rx(r"\bwhy\b|\bkyun\b|\bkyon\b|\brules?\b|policy|kaise|how\s+(was|is)")


def detect_intent(question: str, employee_in_question: bool) -> tuple[Intent, str | None]:
    """(intent, matched text). Rule questions without an employee go to KNOWLEDGE."""
    for intent, pattern in _RULES:
        match = pattern.search(question)
        if not match:
            continue
        if intent is Intent.QA_EMPLOYEE and not employee_in_question and re.search(
            r"payroll|\b20\d{2}\b|month", question, re.IGNORECASE
        ):
            return Intent.QA_PERIOD, match.group(0)
        is_rule_question = intent in INTENT_MODULE and KNOWLEDGE_WORDS.search(question)
        if not employee_in_question and is_rule_question:
            return Intent.KNOWLEDGE, match.group(0)
        return intent, match.group(0)
    if KNOWLEDGE_WORDS.search(question):
        return Intent.KNOWLEDGE, None
    return Intent.GENERAL, None


def knowledge_module(question: str) -> str | None:
    """Best KB module for a free-text policy question."""
    for intent, pattern in _RULES:
        if intent in INTENT_MODULE and pattern.search(question):
            return INTENT_MODULE[intent]
    return None
