"""Who is acting (actor) and which request, carried through calls via contextvars."""

import getpass
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

_actor: ContextVar[str | None] = ContextVar("audit_actor", default=None)
_request_id: ContextVar[str | None] = ContextVar("audit_request_id", default=None)


def default_actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 - no user name available
        return "cli:unknown"


def current_actor() -> str:
    return _actor.get() or default_actor()


def current_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def audit_context(actor: str | None = None, request_id: str | None = None) -> Iterator[str]:
    """Set actor/request id for everything audited inside the block."""
    request_id = request_id or uuid4().hex[:16]
    actor_token = _actor.set(actor or current_actor())
    request_token = _request_id.set(request_id)
    try:
        yield request_id
    finally:
        _actor.reset(actor_token)
        _request_id.reset(request_token)
