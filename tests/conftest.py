from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from payroll_copilot.core.config import PROJECT_ROOT, Settings, get_settings, jwt_signing_key
from payroll_copilot.db.base import Base
from payroll_copilot.db.session import create_db_engine, get_engine, get_session_factory

RULES_DIR = PROJECT_ROOT / "config" / "rules"
TEST_JWT_SECRET = "test-secret-" + "x" * 40

_ENV_VARS = [
    "APP_ENV", "LOG_LEVEL", "LOG_FORMAT", "DATABASE_URL", "JWT_SECRET", "JWT_EXPIRE_MINUTES",
    "LLM_PROVIDER", "LLM_MAX_STEPS", "OLLAMA_MODEL", "OLLAMA_TEMPERATURE", "OLLAMA_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "DEFAULT_PAID_DAYS_BASIS", "DEFAULT_ROUNDING_MODE", "ALLOW_UNVERIFIED_RULES",
    "LOP_DIVISOR", "LOP_BASE", "LEAVE_ENTITLEMENTS", "CORS_ORIGINS", "KB_EMBEDDING_MODEL",
    "KB_MIN_SIMILARITY",
]  # fmt: skip


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Keep the developer's real environment/.env/database out of every test."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("CHECKPOINT_DB", ":memory:")  # never the real data/checkpoints.db
    for cache in (get_settings, get_engine, jwt_signing_key):
        cache.cache_clear()
    yield
    for cache in (get_settings, get_engine, jwt_signing_key):
        cache.cache_clear()


@pytest.fixture
def make_settings():
    """Build Settings from explicit values only (never reads .env)."""

    def _make(**overrides) -> Settings:
        return Settings(_env_file=None, **overrides)

    return _make


def _memory_engine() -> Engine:
    import payroll_copilot.db.models  # noqa: F401 - registers all tables

    return create_db_engine("sqlite:///:memory:")


@pytest.fixture
def engine() -> Iterator[Engine]:
    engine = _memory_engine()
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    session = get_session_factory(engine)()
    yield session
    session.rollback()
    session.close()


@pytest.fixture(scope="session")
def _seeded_template() -> Iterator[Engine]:
    """Rules + demo company, built once per test run (bcrypt hashing is slow on purpose)."""
    from payroll_copilot.db.seed import seed_demo_data
    from payroll_copilot.rules.loader import load_rule_files, sync_rules

    template = _memory_engine()
    Base.metadata.create_all(template)
    from payroll_copilot.services.company_config import sync_defaults
    from payroll_copilot.services.statutory_rates import sync_rates

    with get_session_factory(template)() as s:
        sync_rules(s, load_rule_files(RULES_DIR))
        sync_rates(s)
        sync_defaults(s)
        seed_demo_data(s, dummy_employees=0)  # the 6 named people; dummies tested apart
        s.commit()
    yield template
    template.dispose()


@pytest.fixture
def seeded_engine(_seeded_template) -> Iterator[Engine]:
    """A private copy of the seeded template DB for one test."""
    engine = _memory_engine()
    source, target = _seeded_template.raw_connection(), engine.raw_connection()
    try:
        source.driver_connection.backup(target.driver_connection)
    finally:
        source.close()
        target.close()
    yield engine
    engine.dispose()


@pytest.fixture
def seeded_session(seeded_engine) -> Iterator[Session]:
    session = get_session_factory(seeded_engine)()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def temp_db_url(tmp_path, monkeypatch) -> str:
    """Point DATABASE_URL at a throw-away SQLite file for CLI/migration tests."""
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    return url
