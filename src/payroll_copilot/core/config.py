"""Application settings, loaded from environment variables and ``.env``.

Secrets are ``SecretStr`` so they never appear in ``repr()``, logs or
``Settings.summary()``. Relative paths are resolved against the project root,
so the app behaves the same regardless of the current working directory.
"""

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from payroll_copilot.core.exceptions import ConfigurationError
from payroll_copilot.core.money import RoundingMode
from payroll_copilot.domain.enums import LopAllocation, PaidDaysBasis

# src/payroll_copilot/core/config.py -> project root is three levels above the package dir
PROJECT_ROOT = Path(__file__).resolve().parents[3]

_SQLITE_PREFIX = "sqlite:///"


class LLMProvider(StrEnum):
    NONE = "none"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    AZURE_OPENAI = "azure_openai"
    OLLAMA = "ollama"


class EmbeddingProvider(StrEnum):
    LOCAL = "local"
    OPENAI = "openai"
    OLLAMA = "ollama"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- App ----------
    app_name: str = "AI Payroll Copilot"
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"

    # ---------- Database ----------
    database_url: str = f"{_SQLITE_PREFIX}./data/payroll.db"

    # ---------- LLM ----------
    llm_provider: LLMProvider = LLMProvider.NONE
    llm_model: str | None = None
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str | None = None
    ollama_base_url: str = "http://localhost:11434"

    # ---------- RAG ----------
    embedding_provider: EmbeddingProvider = EmbeddingProvider.LOCAL
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    knowledge_base_dir: Path = Path("./knowledge_base")
    vector_store_dir: Path = Path("./data/chroma")

    # ---------- Rules ----------
    rules_dir: Path = Path("./config/rules")
    allow_unverified_rules: bool = True

    # ---------- Payroll defaults ----------
    default_paid_days_basis: PaidDaysBasis = PaidDaysBasis.CALENDAR
    default_rounding_mode: RoundingMode = RoundingMode.HALF_UP
    default_lop_allocation: LopAllocation = LopAllocation.LAST_SEGMENT

    # ---------- F&F (company policy, not statutory) ----------
    # Heads whose full-month amount forms the per-day rate; "GROSS" = all gross earnings.
    fnf_leave_encashment_heads: list[str] = ["BASIC"]
    fnf_leave_encashment_divisor: Decimal = Field(default=Decimal(30), gt=0)
    fnf_notice_recovery_heads: list[str] = ["GROSS"]
    fnf_notice_recovery_divisor: Decimal = Field(default=Decimal(30), gt=0)
    comparison_tolerance: Decimal = Field(default=Decimal("0"), ge=0)

    # ---------- API ----------
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_key: SecretStr | None = None
    api_base_url: str = "http://127.0.0.1:8000"

    # ---------- Agent memory / audit ----------
    checkpoint_db: Path = Path("./data/checkpoints.db")
    audit_enabled: bool = True

    # ---------- Approvals ----------
    # Users allowed to approve write actions; empty => nobody can approve (safe default).
    approvers: list[str] = []

    @field_validator(
        "llm_model",
        "openai_api_key",
        "anthropic_api_key",
        "azure_openai_api_key",
        "azure_openai_endpoint",
        "azure_openai_api_version",
        "api_key",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        # ".env.example" style empty assignments (KEY=) mean "not set".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("knowledge_base_dir", "vector_store_dir", "rules_dir", "checkpoint_db")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("database_url")
    @classmethod
    def _resolve_sqlite_path(cls, value: str) -> str:
        if not value.startswith(_SQLITE_PREFIX) or value == f"{_SQLITE_PREFIX}:memory:":
            return value
        path = Path(value.removeprefix(_SQLITE_PREFIX))
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        return f"{_SQLITE_PREFIX}{path.as_posix()}"

    @model_validator(mode="after")
    def _prod_requires_complete_config(self) -> "Settings":
        if self.app_env == "prod":
            problems = self.config_issues()
            if self.api_key is None:
                problems.append("API_KEY must be set in prod")
            if problems:
                raise ConfigurationError("; ".join(problems))
        return self

    # ------------------------------------------------------------------ helpers

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider is not LLMProvider.NONE

    def config_issues(self) -> list[str]:
        """Human-readable problems that would stop a feature from working.

        Not raised in dev so that deterministic payroll features work without an
        LLM key; the agent layer checks this before starting.
        """
        issues: list[str] = []
        provider = self.llm_provider
        if provider is LLMProvider.OPENAI and self.openai_api_key is None:
            issues.append("LLM_PROVIDER=openai but OPENAI_API_KEY is not set")
        if provider is LLMProvider.ANTHROPIC and self.anthropic_api_key is None:
            issues.append("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
        if provider is LLMProvider.AZURE_OPENAI:
            if self.azure_openai_api_key is None:
                issues.append("LLM_PROVIDER=azure_openai but AZURE_OPENAI_API_KEY is not set")
            if not self.azure_openai_endpoint:
                issues.append("LLM_PROVIDER=azure_openai but AZURE_OPENAI_ENDPOINT is not set")
        if self.llm_enabled and not self.llm_model:
            issues.append(f"LLM_PROVIDER={provider.value} but LLM_MODEL is not set")
        if self.embedding_provider is EmbeddingProvider.OPENAI and self.openai_api_key is None:
            issues.append("EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is not set")
        return issues

    def secret_values(self) -> list[str]:
        """Raw secret strings, used by the logging redaction filter."""
        secrets = [
            self.openai_api_key,
            self.anthropic_api_key,
            self.azure_openai_api_key,
            self.api_key,
        ]
        return [s.get_secret_value() for s in secrets if s is not None and s.get_secret_value()]

    def summary(self) -> dict[str, str]:
        """Safe, printable view of the configuration (secrets masked)."""
        result: dict[str, str] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                shown = "****" + value.get_secret_value()[-4:] if value.get_secret_value() else ""
            elif value is None:
                shown = "<not set>"
            else:
                shown = str(value.value if isinstance(value, StrEnum) else value)
            if name == "database_url":
                shown = _mask_db_password(shown)
            result[name] = shown
        return result


def _mask_db_password(url: str) -> str:
    """Hide the password in 'scheme://user:password@host/db'."""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        creds = creds.split(":", 1)[0] + ":****"
    return f"{scheme}://{creds}@{host}"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton. Tests call ``get_settings.cache_clear()``."""
    return Settings()
