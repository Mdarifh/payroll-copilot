"""AI payroll audit runs: one row per audit of a month (results stored as JSON)."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin


class AuditRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "audit_runs"
    __table_args__ = (Index("ix_audit_runs_period", "period"),)

    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    period: Mapped[str] = mapped_column(String(7))
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="RUNNING")  # RUNNING|COMPLETED|FAILED
    created_by_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    total: Mapped[int] = mapped_column(default=0)
    checked: Mapped[int] = mapped_column(default=0)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    findings: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    anomalies: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    health: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(500))
