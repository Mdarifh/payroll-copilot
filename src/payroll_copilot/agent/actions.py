"""Turn a chat request for a write action into an approval payload (deterministic)."""

import re
from typing import Any

from payroll_copilot.agent.intents import Intent
from payroll_copilot.domain.enums import ActionType

INTENT_ACTION = {
    Intent.ACTION_TDS_OVERRIDE: ActionType.TDS_OVERRIDE,
    Intent.ACTION_ATTENDANCE: ActionType.ATTENDANCE_CORRECTION,
    Intent.ACTION_QA_SAVE: ActionType.SAVE_QA_RUN,
    Intent.ACTION_RULE_VERIFY: ActionType.VERIFY_RULE,
}
YES = {"yes", "y", "haan", "han", "ha", "haa", "confirm", "ok", "okay", "submit", "ji"}
NO = {"no", "n", "nahi", "nahin", "na", "cancel", "nope", "mat"}

_AMOUNT = re.compile(r"\bto\s*(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_LOP = re.compile(r"\blop\b.*?\bto\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_RULE = re.compile(r"\b([A-Z][A-Z0-9-]{1,31})\s*V(\d+)\b")
# a standalone number: not part of an employee code (EMP-009) or a period (2026-09)
_FREE_NUMBER = re.compile(r"(?<![\w./-])(\d[\d,]*(?:\.\d+)?)(?![\w/-])")
_REASON = re.compile(r"\b(?:because|reason|kyunki|as per|due to|evidence|source)\b[:\s]+(.+)$",
                     re.IGNORECASE)  # fmt: skip


def reply_word(text: str) -> str:
    return text.strip().lower().strip(".!?")


def _number(pattern: re.Pattern[str], question: str) -> str | None:
    """Value after 'to' if present, else the last standalone number in the text."""
    match = pattern.search(question)
    if match:
        return match.group(1).replace(",", "")
    text = _REASON.sub("", question)
    numbers = [n for n in _FREE_NUMBER.findall(text) if not re.fullmatch(r"20\d{2}", n)]
    return numbers[-1].replace(",", "") if numbers else None


def parse_action(
    intent: Intent, question: str, entities: dict[str, Any], actor: str
) -> tuple[ActionType, dict[str, Any] | None, str | None]:
    """(action, payload, problem). ``problem`` explains what is missing."""
    action = INTENT_ACTION[intent]
    reason_match = _REASON.search(question)
    reason = reason_match.group(1).strip() if reason_match else f"requested via chat by {actor}"
    match action:
        case ActionType.TDS_OVERRIDE:
            amount = _number(_AMOUNT, question)
            if amount is None:
                return action, None, "kitna TDS? Likhiye jaise 'override TDS ... to 3000'"
            return action, {"employee_code": entities["employee_code"],
                            "period": entities["period"], "amount": amount,
                            "reason": reason}, None  # fmt: skip
        case ActionType.ATTENDANCE_CORRECTION:
            lop = _number(_LOP, question)
            if lop is None:
                return action, None, "kitne LOP days? Likhiye jaise 'correct LOP ... to 2'"
            return action, {"employee_code": entities["employee_code"],
                            "period": entities["period"], "lop_days": lop,
                            "reason": reason}, None  # fmt: skip
        case ActionType.SAVE_QA_RUN:
            return action, {"period": entities["period"]}, None
        case ActionType.VERIFY_RULE:
            rule = _RULE.search(question.upper())
            if not rule:
                return action, None, (
                    "kaunsa rule? Likhiye jaise 'verify rule EPF v2 as per <source>'"
                )
            if not reason_match:
                return action, None, ("verification ke liye source/evidence chahiye, jaise "
                                      "'verify rule EPF v2 as per EPFO notification ...'")
            return action, {"rule_code": rule.group(1), "version": int(rule.group(2)),
                            "evidence": reason}, None
    return action, None, "unsupported action"
