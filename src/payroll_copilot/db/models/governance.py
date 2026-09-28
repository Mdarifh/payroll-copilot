"""Audit trail, human approvals and QA results."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin, enum_column, utc_now
from payroll_copilot.db.types import Money
from payroll_copilot.domain.enums import ApprovalStatus, ComparisonStatus, QARunStatus


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_logs"

    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    tool_name: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"))


class Approval(IdMixin, TimestampMixin, Base):
    __tablename__ = "approvals"

    action_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[ApprovalStatus] = mapped_column(
        enum_column(ApprovalStatus), default=ApprovalStatus.PENDING
    )
    requested_by: Mapped[str] = mapped_column(String(100))
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(1000))  # requester's reason
    comment: Mapped[str | None] = mapped_column(String(1000))  # approver's comment
    preview: Mapped[dict | None] = mapped_column(JSON)  # impact computed at request time
    result: Mapped[dict | None] = mapped_column(JSON)  # execution outcome / error
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QARun(IdMixin, TimestampMixin, Base):
    __tablename__ = "qa_runs"

    period: Mapped[str] = mapped_column(String(7))
    status: Mapped[QARunStatus] = mapped_column(
        enum_column(QARunStatus), default=QARunStatus.RUNNING
    )
    employees_checked: Mapped[int] = mapped_column(default=0)
    issues_found: Mapped[int] = mapped_column(default=0)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


class QAIssue(IdMixin, Base):
    __tablename__ = "qa_issues"

    qa_run_id: Mapped[int] = mapped_column(ForeignKey("qa_runs.id"))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    component: Mapped[str] = mapped_column(String(32))
    expected: Mapped[Decimal | None] = mapped_column(Money)
    actual: Mapped[Decimal | None] = mapped_column(Money)
    difference: Mapped[Decimal | None] = mapped_column(Money)
    status: Mapped[ComparisonStatus] = mapped_column(enum_column(ComparisonStatus))
    category: Mapped[str | None] = mapped_column(String(32))
    root_cause: Mapped[str | None] = mapped_column(String(1000))
    trace: Mapped[dict] = mapped_column(JSON, default=dict)
