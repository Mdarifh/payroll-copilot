"""Engine and session management."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from payroll_copilot.core.config import get_settings


def create_db_engine(url: str, echo: bool = False) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite") and ":memory:" in url:
        # one shared connection usable from any thread (FastAPI runs sync endpoints in a
        # thread pool); otherwise every thread would see its own empty in-memory DB
        kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    elif url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, echo=echo, **kwargs)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@lru_cache
def get_engine() -> Engine:
    return create_db_engine(get_settings().database_url)


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)


@contextmanager
def read_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Read-only scope: whatever happens, nothing is committed."""
    session = get_session_factory(engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = get_session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
