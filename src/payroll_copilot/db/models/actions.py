"""Action requests: every payroll change submitted from chat or the UI (Action Center).

PENDING_APPROVAL -> APPLIED | REJECTED | FAILED | CANCELLED. The change and the preview
the requester confirmed are stored; the approver sees a fresh preview before approving.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin

PENDING, APPLIED, REJECTED, FAILED, CANCELLED = (
    "PENDING_APPROVAL", "APPLIED", "REJECTED", "FAILED", "CANCELLED")
STATUSES = (PENDING, APPLIED, REJECTED, FAILED, CANCELLED)


class ActionRequest(IdMixin, TimestampMixin, Base):
    __tablename__ = "action_requests"
    __table_args__ = (Index("ix_action_requests_status", "status"),)

    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    period: Mapped[str] = mapped_column(String(7))
    kind: Mapped[str] = mapped_column(String(16))  # lop | overtime | adjustment | salary
    title: Mapped[str] = mapped_column(String(200))
    change: Mapped[dict[str, Any]] = mapped_column(JSON)
    preview: Mapped[dict[str, Any]] = mapped_column(JSON)  # rows + net_change the requester saw
    preview_id: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(16), default="ui")  # ui | chat | undo
    status: Mapped[str] = mapped_column(String(20), default=PENDING)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(String(500))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # recalculated months, net
    error: Mapped[str | None] = mapped_column(String(500))
