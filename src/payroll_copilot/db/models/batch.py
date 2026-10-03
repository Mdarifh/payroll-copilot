"""Bulk payroll: a batch per HR request, one row per employee (the job queue) and the
LOP review rows HR edits before running.

Progress counts are always computed from ``batch_items`` (never stored counters), so
they stay correct whatever crashes or restarts happen.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin, enum_column
from payroll_copilot.db.types import Days, Money
from payroll_copilot.domain.enums import BatchItemStatus, BatchStatus


class PayrollBatch(IdMixin, TimestampMixin, Base):
    __tablename__ = "payroll_batches"

    batch_id: Mapped[str] = mapped_column(String(32), unique=True)  # public id (uuid hex)
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[BatchStatus] = mapped_column(enum_column(BatchStatus),
                                                default=BatchStatus.REVIEW)  # fmt: skip
    created_by_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    total: Mapped[int] = mapped_column(default=0)  # employees in scope
    ok_count: Mapped[int] = mapped_column(default=0)  # no attendance exceptions
    review_count: Mapped[int] = mapped_column(default=0)  # need an LOP decision
    already_processed: Mapped[int] = mapped_column(default=0)  # processed before the batch
    reason: Mapped[str | None] = mapped_column(String(500))  # common LOP reason
    lop_mode: Mapped[str | None] = mapped_column(String(16))  # APPLY | SKIP_ALL
    run_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runs: Mapped[int] = mapped_column(default=0)  # 1 = first run, +1 per "retry failed"
    # Payroll Runs lifecycle after calculation: VALIDATED -> APPROVED -> FINALIZED
    stage: Mapped[str | None] = mapped_column(String(16))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_by_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchItem(IdMixin, Base):
    """One employee of a batch = one job for the worker."""

    __tablename__ = "batch_items"
    __table_args__ = (
        UniqueConstraint("batch_pk", "employee_id"),
        Index("ix_batch_items_batch_status", "batch_pk", "status"),
    )

    batch_pk: Mapped[int] = mapped_column(ForeignKey("payroll_batches.id"))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    status: Mapped[BatchItemStatus] = mapped_column(enum_column(BatchItemStatus),
                                                    default=BatchItemStatus.PENDING)  # fmt: skip
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    lop_days: Mapped[Decimal | None] = mapped_column(Days)  # LOP applied by this batch
    net_pay: Mapped[Decimal | None] = mapped_column(Money)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchLopItem(IdMixin, Base):
    """An employee with absences without approved leave, waiting for HR's decision."""

    __tablename__ = "batch_lop_items"
    __table_args__ = (UniqueConstraint("batch_pk", "employee_id"),)

    batch_pk: Mapped[int] = mapped_column(ForeignKey("payroll_batches.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    absent_dates: Mapped[list[str]] = mapped_column(JSON)  # ISO dates
    absent_days: Mapped[Decimal] = mapped_column(Days)
    suggested_days: Mapped[Decimal] = mapped_column(Days)
    lop_days: Mapped[Decimal] = mapped_column(Days)  # HR's current choice
    apply: Mapped[bool] = mapped_column(default=True)
    base_amount: Mapped[Decimal] = mapped_column(Money)  # monthly gross or basic (config)
    divisor: Mapped[Decimal] = mapped_column(Days)  # 30 / calendar / working days
    per_day: Mapped[Decimal] = mapped_column(Money)  # base_amount / divisor


class ValidationResult(IdMixin, Base):
    """One check of a payroll batch: ERROR blocks approval, WARNING needs review, PASS ok."""

    __tablename__ = "validation_results"
    __table_args__ = (Index("ix_validation_results_batch", "batch_pk", "severity"),)

    batch_pk: Mapped[int] = mapped_column(ForeignKey("payroll_batches.id"))
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    severity: Mapped[str] = mapped_column(String(8))  # ERROR | WARNING | PASS
    code: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
