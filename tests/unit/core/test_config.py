from decimal import Decimal

import pytest
from pydantic import ValidationError

from payroll_copilot.core.config import (
    PROJECT_ROOT,
    LLMProvider,
    PaidDaysBasis,
    Settings,
    get_settings,
)
from payroll_copilot.core.exceptions import ConfigurationError
from payroll_copilot.core.money import RoundingMode


def test_defaults(make_settings):
    settings = make_settings()
    assert settings.app_env == "dev"
    assert settings.llm_provider is LLMProvider.NONE
    assert not settings.llm_enabled
    assert settings.default_paid_days_basis is PaidDaysBasis.CALENDAR
    assert settings.default_rounding_mode is RoundingMode.HALF_UP
    assert settings.comparison_tolerance == Decimal("0")
    assert settings.is_sqlite
    assert settings.config_issues() == []


def test_env_vars_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-abcd1234")
    monkeypatch.setenv("COMPARISON_TOLERANCE", "1")
    settings = Settings(_env_file=None)
    assert settings.llm_provider is LLMProvider.ANTHROPIC
    assert settings.comparison_tolerance == Decimal("1")
    assert settings.config_issues() == []


def test_blank_env_values_mean_not_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "  ")
    settings = Settings(_env_file=None)
    assert settings.openai_api_key is None
    assert settings.llm_model is None


def test_relative_paths_resolve_to_project_root(make_settings):
    settings = make_settings(rules_dir="./config/rules")
    assert settings.rules_dir == (PROJECT_ROOT / "config" / "rules").resolve()
    assert settings.knowledge_base_dir.is_absolute()


def test_sqlite_url_resolved_to_absolute(make_settings):
    url = make_settings().database_url
    assert url.startswith("sqlite:///")
    assert url.endswith("/data/payroll.db")
    assert (PROJECT_ROOT / "data").as_posix() in url


def test_non_sqlite_url_untouched(make_settings):
    url = "postgresql+psycopg://payroll:secret@db:5432/payroll"
    settings = make_settings(database_url=url)
    assert settings.database_url == url
    assert not settings.is_sqlite


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"llm_provider": "openai", "llm_model": "m"}, "OPENAI_API_KEY"),
        ({"llm_provider": "anthropic", "llm_model": "m"}, "ANTHROPIC_API_KEY"),
        ({"llm_provider": "azure_openai", "llm_model": "m"}, "AZURE_OPENAI_ENDPOINT"),
        ({"llm_provider": "ollama"}, "LLM_MODEL"),
        ({"embedding_provider": "openai"}, "OPENAI_API_KEY"),
    ],
)
def test_config_issues(make_settings, overrides, expected_fragment):
    issues = make_settings(**overrides).config_issues()
    assert any(expected_fragment in issue for issue in issues)


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_provider": "gemini-pro-max"},
        {"default_paid_days_basis": "FIXED_31"},
        {"default_rounding_mode": "ROUND_ANYHOW"},
        {"comparison_tolerance": "-1"},
        {"api_port": 70000},
    ],
)
def test_invalid_values_rejected(make_settings, overrides):
    with pytest.raises(ValidationError):
        make_settings(**overrides)


def test_prod_fails_fast_on_incomplete_config(make_settings):
    with pytest.raises(ConfigurationError, match="API_KEY"):
        make_settings(app_env="prod", llm_provider="openai", llm_model="m")


def test_prod_ok_when_complete(make_settings):
    settings = make_settings(
        app_env="prod", llm_provider="openai", llm_model="m",
        openai_api_key="sk-real-9999", api_key="api-key-7777",
    )  # fmt: skip
    assert settings.app_env == "prod"


def test_secrets_never_leak(make_settings):
    secret = "sk-supersecret-abcd1234"
    settings = make_settings(
        openai_api_key=secret,
        database_url="postgresql://payroll:dbpass@host:5432/payroll",
    )
    summary = settings.summary()
    assert secret not in repr(settings)
    assert secret not in str(summary)
    assert summary["openai_api_key"] == "****1234"
    assert "dbpass" not in summary["database_url"]
    assert summary["database_url"] == "postgresql://payroll:****@host:5432/payroll"
    assert settings.secret_values() == [secret]


def test_get_settings_is_cached():
    assert get_settings() is get_settings()
