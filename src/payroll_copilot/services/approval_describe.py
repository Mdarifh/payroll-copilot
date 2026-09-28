"""Human-readable description of an approval action (dependency-free)."""

from typing import Any

from payroll_copilot.domain.enums import ActionType


def describe(action: ActionType | str, payload: dict[str, Any]) -> str:
    action = ActionType(action)
    match action:
        case ActionType.TDS_OVERRIDE:
            return (f"Override monthly TDS of {payload['employee_code']} for {payload['period']} "
                    f"to ₹{payload['amount']}")  # fmt: skip
        case ActionType.ATTENDANCE_CORRECTION:
            return (f"Set LOP of {payload['employee_code']} for {payload['period']} to "
                    f"{payload['lop_days']} days")  # fmt: skip
        case ActionType.SAVE_QA_RUN:
            return f"Save the QA run for {payload['period']}"
        case ActionType.VERIFY_RULE:
            return f"Mark rule {payload['rule_code']} v{payload['version']} as VERIFIED"
    return action.value
