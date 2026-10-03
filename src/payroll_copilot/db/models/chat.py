"""Saved chat conversations (history survives restarts) and their follow-up context."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from payroll_copilot.db.base import Base, IdMixin, TimestampMixin


class ChatSession(IdMixin, TimestampMixin, Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (UniqueConstraint("user_id", "public_id"),
                      Index("ix_chat_sessions_user", "user_id", "updated_at"))  # fmt: skip

    public_id: Mapped[str] = mapped_column(String(64))  # the conversation id the UI uses
    user_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    title: Mapped[str] = mapped_column(String(120))
    # last employee / months / component / intent, for follow-ups ("aur uska PF?")
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ChatMessage(IdMixin, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_session", "session_id"),)

    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(12))  # user | assistant
    text: Mapped[str] = mapped_column(String(4000))
    cards: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
