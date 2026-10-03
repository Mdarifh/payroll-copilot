from decimal import Decimal

import pytest
from pydantic import ValidationError

from payroll_copilot.core.config import (
    PROJECT_ROOT,
    LLMProvider,
    LopBase,
    LopDivisor,
    PaidDaysBasis,
    Settings,
    get_settings,
    jwt_signing_key,
)
from payroll_copilot.core.exceptions import ConfigurationError
from payroll_copilot.core.money import RoundingMode

GOOD_SECRET = "a" * 32


def test_defaults(make_settings):
    settings = make_settings(jwt_secret=None)
    assert settings.app_env == "dev"
    assert settings.llm_provider is LLMProvider.OLLAMA and settings.llm_enabled
    assert settings.llm_model == "qwen2.5:7b"
    assert settings.anthropic_model == "claude-sonnet-5" and settings.anthropic_api_key is None
    assert settings.default_paid_days_basis is PaidDaysBasis.CALENDAR
    assert settings.default_rounding_mode is RoundingMode.HALF_UP
    assert (settings.lop_divisor, settings.lop_base) == (LopDivisor.CALENDAR, LopBase.GROSS)
    assert settings.leave_entitlements == {"CL": 12, "SL": 12, "EL": 18}
    assert settings.is_sqlite
    assert settings.config_issues() == []


def test_env_vars_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("LOP_DIVISOR", "WORKING_DAYS")
    monkeypatch.setenv("LOP_BASE", "BASIC")
    monkeypatch.setenv("LEAVE_ENTITLEMENTS", '{"CL": 10, "SL": 7}')
    settings = Settings(_env_file=None)
    assert not settings.llm_enabled
    assert (settings.lop_divisor, settings.lop_base) == (LopDivisor.WORKING_DAYS, LopBase.BASIC)
    assert settings.leave_entitlements == {"CL": Decimal(10), "SL": Decimal(7)}


def test_blank_jwt_secret_means_not_set(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "  ")
    assert Settings(_env_file=None).jwt_secret is None


def test_relative_paths_resolve_to_project_root(make_settings):
    settings = make_settings(rules_dir="./config/rules")
    assert settings.rules_dir == (PROJECT_ROOT / "config" / "rules").resolve()
    assert settings.salary_heads_file.is_absolute() and settings.salary_heads_file.exists()


def test_sqlite_url_resolved_to_absolute(make_settings):
    url = make_settings().database_url
    assert url.startswith("sqlite:///")
    assert url.endswith("/data/chatbot.db")
    assert (PROJECT_ROOT / "data").as_posix() in url


def test_non_sqlite_url_untouched(make_settings):
    url = "postgresql+psycopg://payroll:secret@db:5432/payroll"
    settings = make_settings(database_url=url)
    assert settings.database_url == url
    assert not settings.is_sqlite


def test_anthropic_needs_an_api_key(make_settings):
    issues = make_settings(llm_provider="anthropic").config_issues()
    assert any("ANTHROPIC_API_KEY" in i for i in issues)
    settings = make_settings(llm_provider="anthropic", anthropic_api_key="sk-ant-test-9876")
    assert settings.config_issues() == [] and settings.llm_model == "claude-sonnet-5"
    assert settings.summary()["anthropic_api_key"] == "****9876"
    assert "sk-ant-test-9876" in settings.secret_values()


def test_short_jwt_secret_is_an_issue(make_settings):
    assert any("at least 32" in i for i in make_settings(jwt_secret="short").config_issues())


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_provider": "gemini-pro-max"},
        {"default_paid_days_basis": "FIXED_31"},
        {"default_rounding_mode": "ROUND_ANYHOW"},
        {"lop_divisor": "FIXED_31"},
        {"lop_base": "CTC"},
        {"jwt_expire_minutes": 1},
        {"api_port": 70000},
    ],
)
def test_invalid_values_rejected(make_settings, overrides):
    with pytest.raises(ValidationError):
        make_settings(**overrides)


def test_prod_needs_a_strong_jwt_secret(make_settings):
    with pytest.raises(ConfigurationError, match="JWT_SECRET must be set"):
        make_settings(app_env="prod", jwt_secret=None)
    with pytest.raises(ConfigurationError, match="at least 32"):
        make_settings(app_env="prod", jwt_secret="short")
    assert make_settings(app_env="prod", jwt_secret=GOOD_SECRET).app_env == "prod"


def test_secrets_never_leak(make_settings):
    secret = "jwt-supersecret-" + "z" * 16 + "abcd"
    settings = make_settings(
        jwt_secret=secret, database_url="postgresql://payroll:dbpass@host:5432/payroll",
    )  # fmt: skip
    summary = settings.summary()
    assert secret not in repr(settings)
    assert secret not in str(summary)
    assert summary["jwt_secret"] == "****abcd"
    assert summary["database_url"] == "postgresql://payroll:****@host:5432/payroll"
    assert settings.secret_values() == [secret]


def test_dev_without_jwt_secret_gets_a_random_key(monkeypatch):
    monkeypatch.delenv("JWT_SECRET")
    get_settings.cache_clear()
    jwt_signing_key.cache_clear()
    key = jwt_signing_key()
    assert len(key) >= 32 and jwt_signing_key() == key  # stable within the process


def test_get_settings_is_cached():
    assert get_settings() is get_settings()
