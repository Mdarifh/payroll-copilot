"""Write audit_log rows: who, what, when, old value, new value.

The row is added to the caller's session, so it commits (or rolls back) together
with the change it describes: no change without its audit entry.
"""

from typing import Any

from pydantic_core import to_jsonable_python
from sqlalchemy.orm import Session

from payroll_copilot.db.models import AuditLog, Employee


def _json(value: Any) -> Any:
    return None if value is None else to_jsonable_python(value)  # Decimal/date -> str


def entry(
    action: str,
    *,
    actor: Employee | None,
    entity: str | None = None,
    entity_id: Any = None,
    old: dict[str, Any] | None = None,
    new: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AuditLog:
    """An audit row, not yet added to a session."""
    return AuditLog(
        actor_id=actor.id if actor else None,
        actor_role=actor.role.value if actor else None,
        action=action, entity=entity,
        entity_id=None if entity_id is None else str(entity_id),
        old_value=_json(old), new_value=_json(new), request_id=request_id,
    )  # fmt: skip


def record(session: Session, action: str, **fields: Any) -> AuditLog:
    """Add an audit row to the caller's session (commits with the change it describes)."""
    row = entry(action, **fields)
    session.add(row)
    return row
