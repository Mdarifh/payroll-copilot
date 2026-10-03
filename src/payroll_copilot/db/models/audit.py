"""Audit log: who did what, when, with the old and new value."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, utc_now


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity", "entity", "entity_id"),
        Index("ix_audit_log_at", "at"),
    )

    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))  # NULL = system
    actor_role: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))  # auth.login, lop.update, payroll.run ...
    entity: Mapped[str | None] = mapped_column(String(64))  # employee, lop_record, ...
    entity_id: Mapped[str | None] = mapped_column(String(64))
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(String(64))
