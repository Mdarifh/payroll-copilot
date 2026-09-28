"""Programmatic Alembic helpers (used by the CLI and tests)."""

from alembic import command
from alembic.config import Config

from payroll_copilot.core.config import PROJECT_ROOT, get_settings


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    # configparser treats '%' as interpolation; escape it (e.g. in DB passwords)
    cfg.set_main_option("sqlalchemy.url", (url or get_settings().database_url).replace("%", "%%"))
    cfg.attributes["configure_logging"] = False
    return cfg


def upgrade_to_head(url: str | None = None) -> None:
    command.upgrade(alembic_config(url), "head")
