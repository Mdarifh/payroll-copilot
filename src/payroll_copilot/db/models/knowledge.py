"""Knowledge base for RAG: policy / rule documents and their searchable chunks.

Kept apart from employee and payroll data: these tables hold documents only. Metadata
(tenant, document type, policy type, access roles, effective dates) filters retrieval.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin


class KnowledgeDocument(IdMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (UniqueConstraint("tenant_id", "path"),)

    tenant_id: Mapped[str] = mapped_column(String(32), default="default")
    path: Mapped[str] = mapped_column(String(300))  # relative to knowledge_base/
    title: Mapped[str] = mapped_column(String(200))
    policy_type: Mapped[str] = mapped_column(String(32))  # module: pf, pt, leave, overtime, ...
    document_type: Mapped[str] = mapped_column(String(32))  # policy | rule | sop
    rule_name: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[str | None] = mapped_column(String(16))
    jurisdiction: Mapped[str] = mapped_column(String(16), default="IN")
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))  # VERIFIED | UNVERIFIED
    access_roles: Mapped[list[str]] = mapped_column(JSON)  # roles that may retrieve it
    content_hash: Mapped[str] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(default=0)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class KnowledgeChunk(IdMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_knowledge_chunks_document", "document_id"),)

    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id",
                                                        ondelete="CASCADE"))  # fmt: skip
    seq: Mapped[int]
    heading: Mapped[str] = mapped_column(String(300))  # "Title > Section"
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
