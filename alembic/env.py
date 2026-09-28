"""Alembic environment: uses DATABASE_URL from settings unless a URL is passed in."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

import payroll_copilot.db.models  # noqa: F401 - registers all tables
from payroll_copilot.core.config import get_settings
from payroll_copilot.db.base import Base
from payroll_copilot.db.session import create_db_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _url()
    # SQLite batch mode rebuilds tables (copy, drop, rename). With FK enforcement on,
    # dropping a referenced table fails, and PRAGMA foreign_keys cannot be changed
    # inside a transaction - so migrate with a plain engine (SQLite FKs off by default)
    # and verify integrity afterwards.
    engine = create_engine(url) if url.startswith("sqlite") else create_db_engine(url)
    with engine.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        if sqlite:
            problems = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise RuntimeError(f"Foreign key violations after migration: {problems}")
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
