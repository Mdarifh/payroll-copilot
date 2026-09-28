import pytest
from sqlalchemy.orm import Session

from payroll_copilot.core.config import PROJECT_ROOT, Settings, get_settings
from payroll_copilot.db.base import Base
from payroll_copilot.db.session import create_db_engine, get_engine, get_session_factory

RULES_DIR = PROJECT_ROOT / "config" / "rules"

_ENV_VARS = [
    "APP_ENV", "LOG_LEVEL", "LOG_FORMAT", "DATABASE_URL",
    "LLM_PROVIDER", "LLM_MODEL", "LLM_TEMPERATURE",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_VERSION",
    "EMBEDDING_PROVIDER", "API_KEY", "COMPARISON_TOLERANCE",
    "DEFAULT_PAID_DAYS_BASIS", "DEFAULT_ROUNDING_MODE", "ALLOW_UNVERIFIED_RULES",
]  # fmt: skip


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Keep the developer's real environment/.env/database/memory out of every test."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()


@pytest.fixture
def make_settings():
    """Build Settings from explicit values only (never reads .env)."""

    def _make(**overrides) -> Settings:
        return Settings(_env_file=None, **overrides)

    return _make


@pytest.fixture
def engine():
    engine = create_db_engine("sqlite:///:memory:")
    import payroll_copilot.db.models  # noqa: F401

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine) -> Session:
    session = get_session_factory(engine)()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def seeded_session(session) -> Session:
    """In-memory DB with all repo rules and the demo company loaded."""
    from payroll_copilot.db.seed import seed_demo_data
    from payroll_copilot.rules.loader import load_rule_files, sync_rules

    sync_rules(session, load_rule_files(RULES_DIR))
    seed_demo_data(session)
    session.commit()
    return session


@pytest.fixture
def fake_embeddings():
    from langchain_core.embeddings import DeterministicFakeEmbedding

    return DeterministicFakeEmbedding(size=64)


@pytest.fixture
def kb_copy(tmp_path):
    """Copy of the repo knowledge base so tests never modify the real one."""
    import shutil

    target = tmp_path / "knowledge_base"
    shutil.copytree(PROJECT_ROOT / "knowledge_base", target)
    return target


@pytest.fixture
def kb(kb_copy, tmp_path, fake_embeddings, make_settings):
    """Knowledge base with generated rule docs, fake embeddings and a temp Chroma store."""
    from payroll_copilot.rag.service import KnowledgeBase

    base = KnowledgeBase(
        settings=make_settings(), embeddings=fake_embeddings,
        persist_dir=tmp_path / "chroma", kb_root=kb_copy,
    )  # fmt: skip
    base.generate()
    base.ingest(rebuild=True)
    return base


@pytest.fixture
def temp_db_url(tmp_path, monkeypatch) -> str:
    """Point DATABASE_URL at a throw-away SQLite file for CLI/migration tests."""
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    get_engine.cache_clear()
    return url
