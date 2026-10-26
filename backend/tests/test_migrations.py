"""Migration from an empty database, drift against the models, and rollback."""
from __future__ import annotations

import uuid

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.db.base import Base
from tests.conftest import alembic_config, assert_safe_database

EXPECTED_TABLES = {
    "profiles", "user_settings", "user_game_profiles",
    "habit_templates", "habits", "habit_versions", "habit_version_days", "habit_logs",
    "projects", "tasks", "task_checklist_items",
    "assessment_versions", "assessment_questions", "assessments", "assessment_answers",
    "improvement_plans", "missions", "mission_logs", "weekly_reviews",
    "xp_rules", "xp_transactions",
}  # fmt: skip

EXPECTED_TRIGGERS = {
    "trg_xp_transactions_append_only", "trg_habit_versions_guard", "trg_habit_version_days_guard",
    "trg_habit_logs_guard", "trg_habits_require_version", "trg_habit_versions_keep_one",
    "trg_mission_logs_guard",
}  # fmt: skip


def _scratch_database(admin_url: str):
    """Create a throw-away database (name ends in `_test`) and return (name, url)."""
    base = make_url(admin_url)
    name = f"atelier_mig_{uuid.uuid4().hex[:8]}_test"
    url = base.set(database=name)
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return name, url.render_as_string(hide_password=False), base


def _drop_database(base, name: str) -> None:
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def test_upgrade_from_empty_database_and_full_rollback(database_url):
    name, url, base = _scratch_database(database_url)
    assert_safe_database(url)
    try:
        cfg = alembic_config(url)
        engine = create_engine(url)
        assert inspect(engine).get_table_names() == []  # truly empty

        command.upgrade(cfg, "head")
        insp = inspect(engine)
        assert EXPECTED_TABLES <= set(insp.get_table_names())
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001"
            triggers = {r[0] for r in conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))}
            extensions = {r[0] for r in conn.execute(text("SELECT extname FROM pg_extension"))}
        assert triggers == EXPECTED_TRIGGERS
        # Overlap/gap-free versions come from the effective_from design: no EXCLUDE needed.
        assert "btree_gist" not in extensions

        command.downgrade(cfg, "base")
        remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
        assert remaining == set()
        with engine.connect() as conn:
            leftovers = conn.execute(
                text("SELECT proname FROM pg_proc WHERE proname LIKE 'atelier\\_%'")
            ).fetchall()
        assert leftovers == []  # downgrade also removes the trigger functions

        command.upgrade(cfg, "head")  # and it can be applied again
        assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())
        engine.dispose()
    finally:
        _drop_database(base, name)


def test_models_and_migration_have_no_drift(engine):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_every_foreign_key_column_is_covered_by_an_index(engine):
    """'Usa índices para claves foráneas': the first column of each FK leads some index/UQ/PK."""
    insp = inspect(engine)
    missing = []
    for table in sorted(EXPECTED_TABLES):
        leading = {tuple(i["column_names"])[:1] for i in insp.get_indexes(table)}
        leading |= {tuple(u["column_names"])[:1] for u in insp.get_unique_constraints(table)}
        leading |= {tuple(insp.get_pk_constraint(table)["constrained_columns"])[:1]}
        for fk in insp.get_foreign_keys(table):
            if (fk["constrained_columns"][0],) not in leading:
                missing.append((table, fk["name"], fk["constrained_columns"]))
    assert missing == []
