from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

import payroll_copilot.db.models  # noqa: F401
from payroll_copilot.db.base import Base
from payroll_copilot.db.migrate import alembic_config, upgrade_to_head
from payroll_copilot.db.session import create_db_engine


def test_migrations_match_models(temp_db_url):
    upgrade_to_head(temp_db_url)
    engine = create_db_engine(temp_db_url)
    with engine.connect() as connection:
        diff = compare_metadata(
            MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata
        )
    engine.dispose()
    assert diff == [], f"Models changed without a migration: {diff}"


def test_upgrade_with_existing_data(temp_db_url):
    """Regression: batch rebuild of a referenced table failed with FK enforcement on."""
    command.upgrade(alembic_config(temp_db_url), "0001")
    engine = create_db_engine(temp_db_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO tds_rules (id, financial_year, regime, standard_deduction, "
            "rebate_marginal_relief, cess_rate, surcharge, rule_code, version, name, "
            "effective_from, status, applicability, content_hash, rounding_mode, created_at, "
            "updated_at) VALUES (1, '2026-27', 'NEW', '75000', 1, '4', '[]', 'T', 1, 't', "
            "'2026-04-01', 'UNVERIFIED', '[]', 'x', 'HALF_UP', '2026-01-01', '2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO tds_slabs (rule_id, sequence, rate) VALUES (1, 0, '0')"
        )
    engine.dispose()

    upgrade_to_head(temp_db_url)

    engine = create_db_engine(temp_db_url)
    with engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(tds_rules)")]
        assert {"deduction_limits", "allow_professional_tax_deduction"} <= set(cols)
        assert conn.exec_driver_sql("SELECT deduction_limits FROM tds_rules").scalar() == "{}"
        assert conn.exec_driver_sql("SELECT count(*) FROM tds_slabs").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        tables = set(inspect(conn).get_table_names())
        assert not any(t.startswith("_alembic_tmp") for t in tables)
    engine.dispose()


def test_downgrade_to_base(temp_db_url):
    upgrade_to_head(temp_db_url)
    command.downgrade(alembic_config(temp_db_url), "base")
    engine = create_db_engine(temp_db_url)
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    engine.dispose()
