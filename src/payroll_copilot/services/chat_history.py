"""Saved conversations: every user message and bot reply (with its cards), per user.

Conversation ids are per user: another user's id never reaches their history (a new,
empty conversation is created instead). Titles come from the first question.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from payroll_copilot.core.exceptions import NotFoundError
from payroll_copilot.db.base import iso_utc
from payroll_copilot.db.models import ChatMessage, ChatSession, Employee

MAX_SESSIONS = 50  # per user; the oldest are removed
LLM_TURNS = 10  # previous exchanges given back to the model after a restart


def _now() -> datetime:
    return datetime.now(UTC)


def get(session: Session, user: Employee, conversation_id: str) -> ChatSession | None:
    return session.scalar(select(ChatSession).where(ChatSession.user_id == user.id,
                                                    ChatSession.public_id == conversation_id))


def get_or_create(session: Session, user: Employee, conversation_id: str,
                  first_message: str) -> ChatSession:  # fmt: skip
    chat = get(session, user, conversation_id)
    if chat is None:
        title = " ".join(first_message.split())[:80] or "New chat"
        chat = ChatSession(public_id=conversation_id, user_id=user.id, title=title, context={})
        session.add(chat)
        session.flush()
        _trim(session, user)
    return chat


def _trim(session: Session, user: Employee) -> None:
    ids = list(session.scalars(select(ChatSession.id).where(ChatSession.user_id == user.id)
                               .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())))
    for old in ids[MAX_SESSIONS:]:
        session.execute(delete(ChatMessage).where(ChatMessage.session_id == old))
        session.execute(delete(ChatSession).where(ChatSession.id == old))


def append(session: Session, chat: ChatSession, user_text: str,
           reply: list[dict[str, Any]], tools: list[str]) -> None:  # fmt: skip
    text = next((m.get("text", "") for m in reply if m.get("type") == "text"), "")
    cards = [m for m in reply if m.get("type") != "text"]
    now = _now()
    session.add_all([
        ChatMessage(session_id=chat.id, role="user", text=user_text[:4000], cards=[], tools=[],
                    created_at=now),
        ChatMessage(session_id=chat.id, role="assistant", text=str(text)[:4000], cards=cards,
                    tools=tools, created_at=now),
    ])  # fmt: skip
    chat.updated_at = now
    session.flush()


def previous_turns(session: Session, chat: ChatSession | None) -> list[tuple[str, str]]:
    """(user text, assistant text) of the last exchanges - to rebuild the model's memory."""
    if chat is None:
        return []
    rows = list(session.scalars(select(ChatMessage).where(ChatMessage.session_id == chat.id)
                                .order_by(ChatMessage.id.desc()).limit(LLM_TURNS * 2)))[::-1]
    turns, pending = [], None
    for m in rows:
        if m.role == "user":
            pending = m.text
        elif pending is not None:
            turns.append((pending, m.text))
            pending = None
    return turns


def list_sessions(session: Session, user: Employee) -> list[dict[str, Any]]:
    counts = dict(session.execute(
        select(ChatMessage.session_id, func.count()).join(ChatSession)
        .where(ChatSession.user_id == user.id).group_by(ChatMessage.session_id)).all())
    rows = session.scalars(select(ChatSession).where(ChatSession.user_id == user.id)
                           .order_by(ChatSession.updated_at.desc()))  # fmt: skip
    return [{"id": c.public_id, "title": c.title, "updated_at": iso_utc(c.updated_at),
             "messages": counts.get(c.id, 0)} for c in rows]


def messages(session: Session, user: Employee, conversation_id: str) -> dict[str, Any]:
    chat = get(session, user, conversation_id)
    if chat is None:
        raise NotFoundError("Conversation not found")
    rows = session.scalars(select(ChatMessage).where(ChatMessage.session_id == chat.id)
                           .order_by(ChatMessage.id))  # fmt: skip
    items = []
    for m in rows:
        if m.role == "user":
            items.append({"from": "user", "text": m.text, "at": iso_utc(m.created_at)})
        else:
            items.append({"from": "bot", "messages": [{"type": "text", "text": m.text},
                                                      *m.cards],
                          "at": iso_utc(m.created_at)})  # fmt: skip
    return {"id": chat.public_id, "title": chat.title, "items": items}


def remove(session: Session, user: Employee, conversation_id: str) -> bool:
    chat = get(session, user, conversation_id)
    if chat is None:
        return False
    session.execute(delete(ChatMessage).where(ChatMessage.session_id == chat.id))
    session.delete(chat)
    session.flush()
    return True
