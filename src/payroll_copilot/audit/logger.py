"""Write and query audit entries.

Entries are written in their own transaction, so read-only work (which always
rolls back) is still audited. For reads a failing audit write is logged and
ignored; ``strict=True`` (write actions) re-raises so the action fails closed.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from payroll_copilot.audit.context import current_actor, current_request_id
from payroll_copilot.audit.redaction import redact
from payroll_copilot.core.config import get_settings
from payroll_copilot.db.models import AuditLog

log = logging.getLogger(__name__)
SessionProvider = Callable[[], AbstractContextManager[Session]]


def rule_versions(payload: Any) -> list[str]:
    """Rule versions referenced anywhere in a result payload, e.g. ['EPF v2', 'PT-MH v1']."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "rule_code" in node and "version" in node and "effective_from" in node:
                label = f"{node['rule_code']} v{node['version']}"
                if label not in found:
                    found.append(label)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


class AuditLogger:
    def __init__(self, sessions: SessionProvider | None = None, enabled: bool = True) -> None:
        if sessions is None:
            from payroll_copilot.db.session import session_scope

            sessions = session_scope
        self.sessions = sessions
        self.enabled = enabled

    def log(
        self,
        action: str,
        *,
        entity: str | None = None,
        entity_id: str | None = None,
        tool_name: str | None = None,
        details: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        actor: str | None = None,
        approval_id: int | None = None,
        strict: bool = False,
    ) -> int | None:
        if not self.enabled:
            return None
        secrets = get_settings().secret_values()
        entry = AuditLog(
            actor=(actor or current_actor())[:100], action=action[:64],
            entity=entity, entity_id=None if entity_id is None else str(entity_id)[:64],
            tool_name=tool_name, request_id=current_request_id(),
            before=redact(before, secrets) if before else None,
            after=redact(details, secrets) if details else None,
            approval_id=approval_id,
        )  # fmt: skip
        try:
            with self.sessions() as session:
                session.add(entry)
                session.flush()
                return entry.id
        except Exception:
            if strict:
                raise
            log.exception("Audit write failed for %s", action)
            return None

    def search(
        self,
        *,
        action: str | None = None,
        actor: str | None = None,
        entity_id: str | None = None,
        request_id: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        if action:
            stmt = stmt.where(AuditLog.action.like(action.replace("*", "%")))
        if actor:
            stmt = stmt.where(AuditLog.actor == actor)
        if entity_id:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        if request_id:
            stmt = stmt.where(AuditLog.request_id == request_id)
        if since:
            stmt = stmt.where(AuditLog.ts >= since)
        with self.sessions() as session:
            return [
                {"id": e.id, "ts": e.ts.isoformat(), "actor": e.actor, "action": e.action,
                 "entity": e.entity, "entity_id": e.entity_id, "tool_name": e.tool_name,
                 "request_id": e.request_id, "details": e.after, "approval_id": e.approval_id}
                for e in session.scalars(stmt)
            ]  # fmt: skip
