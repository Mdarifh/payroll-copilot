"""Application settings, loaded from environment variables and ``.env``.

Secrets are ``SecretStr`` so they never appear in ``repr()``, logs or
``Settings.summary()``. Relative paths are resolved against the project root,
so the app behaves the same regardless of the current working directory.
"""

import logging
import secrets
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
_MIN_JWT_SECRET = 32

log = logging.getLogger(__name__)


class LLMProvider(StrEnum):
    NONE = "none"
    OLLAMA = "ollama"  # free, local
    ANTHROPIC = "anthropic"  # Claude API (needs ANTHROPIC_API_KEY, paid per use)


class LopDivisor(StrEnum):
    """Days one month's pay is divided by to get the LOP per-day rate."""

    FIXED_30 = "FIXED_30"
    CALENDAR = "CALENDAR"  # days in the month
    WORKING_DAYS = "WORKING_DAYS"  # month days minus week offs and holidays


class LopBase(StrEnum):
    """Which monthly amount the LOP per-day rate is taken from."""

    GROSS = "GROSS"
    BASIC = "BASIC"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- App ----------
    app_name: str = "Payroll Chatbot"
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"

    # ---------- Company ----------
    company_name: str = "Demo Tech Pvt Ltd"
    company_state: str = "MH"

    # ---------- Database ----------
    # SQLite for now; PostgreSQL later (postgresql+psycopg://user:pass@host:5432/payroll)
    database_url: str = f"{_SQLITE_PREFIX}./data/chatbot.db"

    # ---------- Auth ----------
    # HS256 signing key for login tokens. Required in prod; in dev a random key is
    # generated per process (everyone is logged out when the API restarts).
    jwt_secret: SecretStr | None = None
    jwt_expire_minutes: int = Field(default=480, ge=5, le=7 * 24 * 60)

    # ---------- LLM (chooses tools and words the answer; never calculates) ----------
    llm_provider: LLMProvider = LLMProvider.OLLAMA
    llm_max_steps: int = Field(default=6, ge=1, le=12)  # model calls per chat message
    ollama_model: str = "qwen2.5:7b"
    ollama_base_url: str = "http://localhost:11434"
    ollama_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5"

    # ---------- API ----------
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    # comma separated origins allowed to call the API from a browser (React dev server)
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # ---------- Rules ----------
    rules_dir: Path = Path("./config/rules")
    salary_heads_file: Path = Path("./config/payroll/salary_structures.yaml")
    allow_unverified_rules: bool = True

    # ---------- Payroll policy (company-specific, never decided by the LLM) ----------
    default_paid_days_basis: PaidDaysBasis = PaidDaysBasis.CALENDAR
    default_rounding_mode: RoundingMode = RoundingMode.HALF_UP
    default_lop_allocation: LopAllocation = LopAllocation.LAST_SEGMENT
    # LOP per day = (GROSS | BASIC) / (30 | calendar days | working days)
    lop_divisor: LopDivisor = LopDivisor.CALENDAR
    lop_base: LopBase = LopBase.GROSS
    # ---------- Bulk payroll (batches) ----------
    batch_page_size: int = Field(default=100, ge=10, le=1000)  # employees read per page
    batch_source_rate: float = Field(default=20.0, gt=0)  # payroll-source calls per second
    batch_chunk_size: int = Field(default=50, ge=1, le=1000)  # employees per worker chunk
    batch_max_attempts: int = Field(default=3, ge=1, le=10)  # retries for temporary errors
    batch_worker_enabled: bool = True  # background worker inside the API process
    # yearly leave entitlement per leave type (days)
    leave_entitlements: dict[str, Decimal] = {
        "CL": Decimal(12), "SL": Decimal(12), "EL": Decimal(18),
    }  # fmt: skip

    @field_validator("jwt_secret", "anthropic_api_key", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        # ".env.example" style empty assignments (KEY=) mean "not set".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("rules_dir", "salary_heads_file")
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
            if self.jwt_secret is None:
                problems.append("JWT_SECRET must be set in prod")
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

    @property
    def llm_model(self) -> str | None:
        return {LLMProvider.OLLAMA: self.ollama_model,
                LLMProvider.ANTHROPIC: self.anthropic_model}.get(self.llm_provider)  # fmt: skip

    def config_issues(self) -> list[str]:
        """Human-readable problems that would stop a feature from working."""
        issues: list[str] = []
        if self.llm_provider is LLMProvider.ANTHROPIC and self.anthropic_api_key is None:
            issues.append("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
        secret = self.jwt_secret
        if secret is not None and len(secret.get_secret_value()) < _MIN_JWT_SECRET:
            issues.append(f"JWT_SECRET must be at least {_MIN_JWT_SECRET} characters")
        return issues + self.lop_issues()

    def lop_issues(self) -> list[str]:
        """LOP shown to HR must equal what payroll deducts, so the policy must match the
        engine: it prorates all gross heads by paid days on DEFAULT_PAID_DAYS_BASIS."""
        issues = []
        if self.lop_base is not LopBase.GROSS:
            issues.append("LOP_BASE=BASIC is not supported by the payroll engine yet "
                          "(it prorates all gross heads); use LOP_BASE=GROSS")  # fmt: skip
        expected = PaidDaysBasis(self.lop_divisor.value)  # same names on both enums
        if self.default_paid_days_basis is not expected:
            issues.append(f"LOP_DIVISOR={self.lop_divisor.value} needs "
                          f"DEFAULT_PAID_DAYS_BASIS={expected.value} (now "
                          f"{self.default_paid_days_basis.value})")  # fmt: skip
        return issues

    def secret_values(self) -> list[str]:
        """Raw secret strings, used by the logging redaction filter."""
        values = [self.jwt_secret, self.anthropic_api_key]
        return [s.get_secret_value() for s in values if s is not None and s.get_secret_value()]

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


@lru_cache
def jwt_signing_key() -> str:
    """JWT_SECRET, or (dev/test only) a random key that lives as long as the process."""
    settings = get_settings()
    if settings.jwt_secret is not None:
        return settings.jwt_secret.get_secret_value()
    log.warning("JWT_SECRET not set: using a random key; logins end when the API restarts")
    return secrets.token_urlsafe(48)
