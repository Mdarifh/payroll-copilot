"""Company payroll configuration: effective-dated key/values (defaults from a config file)."""

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.core.config import PROJECT_ROOT
from payroll_copilot.core.exceptions import PayrollCopilotError
from payroll_copilot.db.models import CompanySetting, Employee
from payroll_copilot.rules.loader import read_yaml
from payroll_copilot.services import audit

SETTINGS_FILE = PROJECT_ROOT / "config" / "company" / "settings.yaml"
ALLOWED: dict[str, tuple] = {
    "pf_wage_policy": ("statutory_ceiling", "actual_wages"),
    "pf_wage_basis": ("BASIC_DA", "STATUTORY_WAGES"),
    "pf_ceiling_scope": ("RULE_PERIOD", "SEGMENT"),
    "pf_proration_divisor": ("CALENDAR", "FIXED_30"),
    "edli_wage_basis": ("PF_WAGE", "CAPPED"),
    "pf_segment_engine": (True, False),
    "payroll_second_approver": (True, False),
    "change_approval": (True, False),
}
# keys added after go-live: used with this default until the company sets them
OPTIONAL_DEFAULTS: dict[str, Any] = {"change_approval": True}
ALIASES = {("pf_wage_policy", "ceiling_15000"): "statutory_ceiling"}


def check_setting(key: str, value: Any) -> Any:
    if key not in ALLOWED:
        raise PayrollCopilotError(f"Unknown company setting {key!r}")
    value = ALIASES.get((key, value), value)
    if value not in ALLOWED[key]:
        raise PayrollCopilotError(f"{key} must be one of {', '.join(map(str, ALLOWED[key]))}")
    return value


def sync_defaults(session: Session) -> int:
    """Insert the file's values for keys the company has never set."""
    data = read_yaml(SETTINGS_FILE)
    added = 0
    for key, value in data["settings"].items():
        if session.scalar(select(CompanySetting.id).where(CompanySetting.key == key)) is None:
            session.add(CompanySetting(key=key, value=check_setting(key, value),
                                       effective_from=data["effective_from"]))  # fmt: skip
            added += 1
    session.flush()
    return added


def get_config(session: Session, on: date) -> dict[str, Any]:
    rows = session.scalars(select(CompanySetting).where(CompanySetting.effective_from <= on)
                           .order_by(CompanySetting.effective_from, CompanySetting.id))
    config: dict[str, Any] = {}
    for row in rows:
        config[row.key] = row.value
    for key, default in OPTIONAL_DEFAULTS.items():
        config.setdefault(key, default)
    missing = [k for k in ALLOWED if k not in config]
    if missing:
        raise PayrollCopilotError(f"Company configuration missing: {', '.join(missing)} "
                                  "(run payroll-chatbot rules load)")  # fmt: skip
    return config


def set_setting(session: Session, key: str, value: Any, effective_from: date,
                actor: Employee) -> CompanySetting:  # fmt: skip
    value = check_setting(key, value)
    old = get_config(session, effective_from).get(key)
    row = session.scalar(select(CompanySetting).where(
        CompanySetting.key == key, CompanySetting.effective_from == effective_from))
    if row is None:
        row = CompanySetting(key=key, effective_from=effective_from)
        session.add(row)
    row.value, row.updated_by_id = value, actor.id
    session.flush()
    audit.record(session, "settings.update", actor=actor, entity="company_setting",
                 entity_id=key, old={"value": old},
                 new={"value": value, "effective_from": effective_from})  # fmt: skip
    return row
