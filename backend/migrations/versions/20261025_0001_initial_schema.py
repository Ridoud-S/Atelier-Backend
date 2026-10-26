"""initial schema

Atelier V1: 21 tables, composite ownership FKs, CHECK/UNIQUE constraints, indexes and a
small set of PL/pgSQL guards (append-only XP ledger, habit-version rules, mission-log dates).

Revision ID: 0001
Revises:
Create Date: 2026-10-25 23:49:12.676542
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


MIN_SERVER_VERSION_NUM = 130000  # PostgreSQL 13: gen_random_uuid() is built in.


def _preflight() -> None:
    """Fail early and clearly if the server cannot run this schema.

    The schema does NOT use EXCLUDE constraints, so `btree_gist` is not required (habit
    versions use a single `effective_from`, which makes overlaps and gaps impossible by
    construction). If a future migration needs it, check availability the same way:
    SELECT 1 FROM pg_available_extensions WHERE name = 'btree_gist'
    """
    if context.is_offline_mode():  # `alembic upgrade --sql` has no server to inspect
        return
    bind = op.get_bind()
    version_num = int(bind.execute(sa.text("SHOW server_version_num")).scalar_one())
    if version_num < MIN_SERVER_VERSION_NUM:
        raise RuntimeError(
            f"Atelier needs PostgreSQL 13 or newer (server_version_num={version_num})."
        )
    bind.execute(sa.text("SELECT gen_random_uuid()"))  # raises if unavailable


# ---------------------------------------------------------------------------
# Guards that cannot be expressed as declarative constraints.
# Error class 23 (integrity) so drivers raise IntegrityError.
# ---------------------------------------------------------------------------
TRIGGER_STATEMENTS = [
    # 1. XP ledger is append-only. ------------------------------------------------------
    """
    CREATE FUNCTION atelier_xp_transactions_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'xp_transactions is append-only: % is not allowed', TG_OP
            USING ERRCODE = 'integrity_constraint_violation';
    END
    $$
    """,
    """
    CREATE TRIGGER trg_xp_transactions_append_only
        BEFORE UPDATE OR DELETE ON xp_transactions
        FOR EACH ROW EXECUTE FUNCTION atelier_xp_transactions_append_only()
    """,
    # 2. Habit versions: immutability once logged, no retroactive reinterpretation. -----
    """
    CREATE FUNCTION atelier_habit_versions_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
        habit_end date;
    BEGIN
        IF TG_OP = 'UPDATE' THEN
            IF NEW.habit_id IS DISTINCT FROM OLD.habit_id THEN
                RAISE EXCEPTION 'habit_versions.habit_id is immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.effective_from IS NOT DISTINCT FROM OLD.effective_from
               AND NEW.goal_type IS NOT DISTINCT FROM OLD.goal_type
               AND NEW.target_value IS NOT DISTINCT FROM OLD.target_value
               AND NEW.unit IS NOT DISTINCT FROM OLD.unit
               AND NEW.frequency_type IS NOT DISTINCT FROM OLD.frequency_type
               AND NEW.weekly_target_count IS NOT DISTINCT FROM OLD.weekly_target_count THEN
                RETURN NEW;  -- nothing semantic changed (e.g. only updated_at)
            END IF;
            IF EXISTS (SELECT 1 FROM habit_logs WHERE habit_version_id = OLD.id) THEN
                RAISE EXCEPTION 'habit version % is referenced by logs and is immutable', OLD.id
                    USING ERRCODE = 'check_violation';
            END IF;
        END IF;

        -- Serialize with log inserts (they take FOR SHARE) and other version changes.
        SELECT end_date INTO habit_end FROM habits WHERE id = NEW.habit_id FOR UPDATE;

        IF habit_end IS NOT NULL AND NEW.effective_from > habit_end THEN
            RAISE EXCEPTION 'version effective_from % is after the habit end_date %',
                NEW.effective_from, habit_end USING ERRCODE = 'check_violation';
        END IF;

        IF EXISTS (
            SELECT 1 FROM habit_logs
            WHERE habit_id = NEW.habit_id AND occurrence_date >= NEW.effective_from
        ) THEN
            RAISE EXCEPTION 'retroactive version change rejected: habit % has logs on or after %',
                NEW.habit_id, NEW.effective_from USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER trg_habit_versions_guard
        BEFORE INSERT OR UPDATE ON habit_versions
        FOR EACH ROW EXECUTE FUNCTION atelier_habit_versions_guard()
    """,
    # 3. Weekday rows of a version that already has logs cannot change. ----------------
    """
    CREATE FUNCTION atelier_habit_version_days_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP IN ('UPDATE', 'DELETE') THEN
            IF EXISTS (SELECT 1 FROM habit_logs WHERE habit_version_id = OLD.habit_version_id) THEN
                RAISE EXCEPTION 'habit version % has logs; its weekdays are immutable',
                    OLD.habit_version_id USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            IF EXISTS (SELECT 1 FROM habit_logs WHERE habit_version_id = NEW.habit_version_id) THEN
                RAISE EXCEPTION 'habit version % has logs; its weekdays are immutable',
                    NEW.habit_version_id USING ERRCODE = 'check_violation';
            END IF;
        END IF;
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER trg_habit_version_days_guard
        BEFORE INSERT OR UPDATE OR DELETE ON habit_version_days
        FOR EACH ROW EXECUTE FUNCTION atelier_habit_version_days_guard()
    """,
    # 4. A habit log must use the version in force on its date and respect end_date. ---
    """
    CREATE FUNCTION atelier_habit_logs_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
        habit_end date;
        applicable uuid;
    BEGIN
        -- Shared lock: waits while a version change holds FOR UPDATE on the habit row.
        SELECT end_date INTO habit_end FROM habits WHERE id = NEW.habit_id FOR SHARE;

        SELECT id INTO applicable
        FROM habit_versions
        WHERE habit_id = NEW.habit_id AND effective_from <= NEW.occurrence_date
        ORDER BY effective_from DESC
        LIMIT 1;

        IF applicable IS NULL OR applicable <> NEW.habit_version_id THEN
            RAISE EXCEPTION 'occurrence_date % is not governed by habit version %',
                NEW.occurrence_date, NEW.habit_version_id USING ERRCODE = 'check_violation';
        END IF;
        IF habit_end IS NOT NULL AND NEW.occurrence_date > habit_end THEN
            RAISE EXCEPTION 'occurrence_date % is after the habit end_date %',
                NEW.occurrence_date, habit_end USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER trg_habit_logs_guard
        BEFORE INSERT OR UPDATE OF habit_id, habit_version_id, occurrence_date ON habit_logs
        FOR EACH ROW EXECUTE FUNCTION atelier_habit_logs_guard()
    """,
    # 5. Every habit keeps at least one version (checked at COMMIT). -------------------
    """
    CREATE FUNCTION atelier_habit_requires_version() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
        hid uuid;
    BEGIN
        IF TG_TABLE_NAME = 'habits' THEN
            hid := NEW.id;
        ELSE
            hid := OLD.habit_id;
        END IF;
        IF EXISTS (SELECT 1 FROM habits WHERE id = hid)
           AND NOT EXISTS (SELECT 1 FROM habit_versions WHERE habit_id = hid) THEN
            RAISE EXCEPTION 'habit % must have at least one version', hid
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NULL;
    END
    $$
    """,
    """
    CREATE CONSTRAINT TRIGGER trg_habits_require_version
        AFTER INSERT ON habits
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION atelier_habit_requires_version()
    """,
    """
    CREATE CONSTRAINT TRIGGER trg_habit_versions_keep_one
        AFTER DELETE ON habit_versions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION atelier_habit_requires_version()
    """,
    # 6. A mission log date must fall inside the mission's week. ------------------------
    """
    CREATE FUNCTION atelier_mission_logs_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
        ws date;
    BEGIN
        SELECT week_start INTO ws FROM missions WHERE id = NEW.mission_id;
        -- If the mission does not exist the foreign key reports the error.
        IF ws IS NOT NULL AND (NEW.occurrence_date < ws OR NEW.occurrence_date > ws + 6) THEN
            RAISE EXCEPTION 'occurrence_date % is outside the mission week starting %',
                NEW.occurrence_date, ws USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER trg_mission_logs_guard
        BEFORE INSERT OR UPDATE OF mission_id, occurrence_date ON mission_logs
        FOR EACH ROW EXECUTE FUNCTION atelier_mission_logs_guard()
    """,
]

DROP_TRIGGER_STATEMENTS = [
    "DROP TRIGGER IF EXISTS trg_mission_logs_guard ON mission_logs",
    "DROP FUNCTION IF EXISTS atelier_mission_logs_guard()",
    "DROP TRIGGER IF EXISTS trg_habit_versions_keep_one ON habit_versions",
    "DROP TRIGGER IF EXISTS trg_habits_require_version ON habits",
    "DROP FUNCTION IF EXISTS atelier_habit_requires_version()",
    "DROP TRIGGER IF EXISTS trg_habit_logs_guard ON habit_logs",
    "DROP FUNCTION IF EXISTS atelier_habit_logs_guard()",
    "DROP TRIGGER IF EXISTS trg_habit_version_days_guard ON habit_version_days",
    "DROP FUNCTION IF EXISTS atelier_habit_version_days_guard()",
    "DROP TRIGGER IF EXISTS trg_habit_versions_guard ON habit_versions",
    "DROP FUNCTION IF EXISTS atelier_habit_versions_guard()",
    "DROP TRIGGER IF EXISTS trg_xp_transactions_append_only ON xp_transactions",
    "DROP FUNCTION IF EXISTS atelier_xp_transactions_append_only()",
]


def upgrade() -> None:
    _preflight()
    op.create_table('assessment_versions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=120), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('version >= 1', name=op.f('ck_assessment_versions_version_positive')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_versions')),
    sa.UniqueConstraint('version', name='uq_assessment_versions_version')
    )
    op.create_index('uq_assessment_versions_single_active', 'assessment_versions', ['is_active'], unique=True, postgresql_where=sa.text('is_active'))
    op.create_table('habit_templates',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('title', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('category', sa.String(length=20), nullable=False),
    sa.Column('goal_type', sa.String(length=10), nullable=False),
    sa.Column('target_value', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('unit', sa.String(length=32), nullable=True),
    sa.Column('default_weekly_target_count', sa.SmallInteger(), nullable=False),
    sa.Column('difficulty', sa.SmallInteger(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(goal_type = 'binary' AND target_value IS NULL AND unit IS NULL) OR (goal_type = 'quantity' AND target_value IS NOT NULL AND target_value > 0 AND unit IS NOT NULL) OR (goal_type = 'duration' AND target_value IS NOT NULL AND target_value > 0 AND unit = 'minutes')", name=op.f('ck_habit_templates_goal_shape')),
    sa.CheckConstraint("category IN ('sleep', 'hydration', 'nutrition', 'activity', 'focus', 'wellbeing', 'other')", name=op.f('ck_habit_templates_category_valid')),
    sa.CheckConstraint("goal_type IN ('binary', 'quantity', 'duration')", name=op.f('ck_habit_templates_goal_type_valid')),
    sa.CheckConstraint('default_weekly_target_count BETWEEN 1 AND 7', name=op.f('ck_habit_templates_weekly_target_range')),
    sa.CheckConstraint('difficulty BETWEEN 1 AND 5', name=op.f('ck_habit_templates_difficulty_range')),
    sa.CheckConstraint('version >= 1', name=op.f('ck_habit_templates_version_positive')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_habit_templates')),
    sa.UniqueConstraint('code', 'version', name='uq_habit_templates_code_version')
    )
    op.create_index('ix_habit_templates_active_category', 'habit_templates', ['is_active', 'category'], unique=False)
    op.create_table('profiles',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('display_name', sa.String(length=100), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_profiles'))
    )
    op.create_table('xp_rules',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('rule_code', sa.String(length=40), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('source_type', sa.String(length=12), nullable=False),
    sa.Column('xp_amount', sa.Integer(), nullable=False),
    sa.Column('daily_cap', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source_type IN ('habit_log', 'mission_log', 'task')", name=op.f('ck_xp_rules_source_type_valid')),
    sa.CheckConstraint('daily_cap IS NULL OR daily_cap >= 0', name=op.f('ck_xp_rules_daily_cap_nonneg')),
    sa.CheckConstraint('xp_amount >= 0', name=op.f('ck_xp_rules_xp_amount_nonneg')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_xp_rules')),
    sa.UniqueConstraint('rule_code', 'effective_from', name='uq_xp_rules_code_from')
    )
    op.create_table('assessment_questions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('assessment_version_id', sa.UUID(), nullable=False),
    sa.Column('question_key', sa.String(length=64), nullable=False),
    sa.Column('domain', sa.String(length=20), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('answer_type', sa.String(length=16), nullable=False),
    sa.Column('options', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('is_required', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("answer_type IN ('scale', 'number', 'single_choice', 'multi_choice', 'text')", name=op.f('ck_assessment_questions_answer_type_valid')),
    sa.CheckConstraint("domain IN ('sleep', 'hydration', 'nutrition', 'activity', 'focus', 'wellbeing', 'goals', 'availability')", name=op.f('ck_assessment_questions_domain_valid')),
    sa.CheckConstraint('position >= 0', name=op.f('ck_assessment_questions_position_nonneg')),
    sa.ForeignKeyConstraint(['assessment_version_id'], ['assessment_versions.id'], name=op.f('fk_assessment_questions_assessment_version_id_assessment_versions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_questions')),
    sa.UniqueConstraint('assessment_version_id', 'position', name='uq_assessment_questions_version_pos'),
    sa.UniqueConstraint('assessment_version_id', 'question_key', name='uq_assessment_questions_version_key'),
    sa.UniqueConstraint('id', 'assessment_version_id', name='uq_assessment_questions_id_version')
    )
    op.create_table('assessments',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('assessment_version_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=12), server_default='in_progress', nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL)", name=op.f('ck_assessments_completed_at_matches_status')),
    sa.CheckConstraint("status IN ('in_progress', 'completed', 'abandoned')", name=op.f('ck_assessments_status_valid')),
    sa.ForeignKeyConstraint(['assessment_version_id'], ['assessment_versions.id'], name=op.f('fk_assessments_assessment_version_id_assessment_versions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['profiles.id'], name=op.f('fk_assessments_user_id_profiles'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessments')),
    sa.UniqueConstraint('id', 'assessment_version_id', name='uq_assessments_id_version'),
    sa.UniqueConstraint('id', 'user_id', name='uq_assessments_id_user_id')
    )
    op.create_index('ix_assessments_assessment_version_id', 'assessments', ['assessment_version_id'], unique=False)
    op.create_index('ix_assessments_user_id_started_at', 'assessments', ['user_id', 'started_at'], unique=False)
    op.create_index('uq_assessments_user_in_progress', 'assessments', ['user_id'], unique=True, postgresql_where=sa.text("status = 'in_progress'"))
    op.create_table('habits',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('category', sa.String(length=20), nullable=False),
    sa.Column('origin', sa.String(length=10), server_default='user', nullable=False),
    sa.Column('source_template_id', sa.UUID(), nullable=True),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'archived') = (archived_at IS NOT NULL)", name=op.f('ck_habits_archived_at_matches_status')),
    sa.CheckConstraint("category IN ('sleep', 'hydration', 'nutrition', 'activity', 'focus', 'wellbeing', 'other')", name=op.f('ck_habits_category_valid')),
    sa.CheckConstraint("origin = 'system' OR source_template_id IS NULL", name=op.f('ck_habits_template_only_if_system')),
    sa.CheckConstraint("origin IN ('user', 'system')", name=op.f('ck_habits_origin_valid')),
    sa.CheckConstraint("status IN ('active', 'archived')", name=op.f('ck_habits_status_valid')),
    sa.ForeignKeyConstraint(['source_template_id'], ['habit_templates.id'], name=op.f('fk_habits_source_template_id_habit_templates'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['profiles.id'], name=op.f('fk_habits_user_id_profiles'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_habits')),
    sa.UniqueConstraint('id', 'user_id', name='uq_habits_id_user_id')
    )
    op.create_index('ix_habits_source_template_id', 'habits', ['source_template_id'], unique=False)
    op.create_index('ix_habits_user_id_status', 'habits', ['user_id', 'status'], unique=False)
    op.create_index('uq_habits_user_active_name', 'habits', ['user_id', sa.literal_column('lower(name)')], unique=True, postgresql_where=sa.text("status = 'active'"))
    op.create_table('projects',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'archived') = (archived_at IS NOT NULL)", name=op.f('ck_projects_archived_at_matches_status')),
    sa.CheckConstraint("status IN ('active', 'completed', 'archived')", name=op.f('ck_projects_status_valid')),
    sa.ForeignKeyConstraint(['user_id'], ['profiles.id'], name=op.f('fk_projects_user_id_profiles'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_projects')),
    sa.UniqueConstraint('id', 'user_id', name='uq_projects_id_user_id')
    )
    op.create_index('ix_projects_user_id_status', 'projects', ['user_id', 'status'], unique=False)
    op.create_index('uq_projects_user_open_name', 'projects', ['user_id', sa.literal_column('lower(name)')], unique=True, postgresql_where=sa.text("status <> 'archived'"))
    op.create_table('user_game_profiles',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('total_xp', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('total_xp >= 0', name=op.f('ck_user_game_profiles_total_xp_nonneg')),
    sa.ForeignKeyConstraint(['user_id'], ['profiles.id'], name=op.f('fk_user_game_profiles_user_id_profiles'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_user_game_profiles'))
    )
    op.create_table('user_settings',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('timezone', sa.String(length=64), server_default='UTC', nullable=False),
    sa.Column('language', sa.String(length=10), server_default='es', nullable=False),
    sa.Column('theme', sa.String(length=10), server_default='system', nullable=False),
    sa.Column('week_start_day', sa.SmallInteger(), server_default='1', nullable=False),
    sa.Column('time_format', sa.String(length=3), server_default='24h', nullable=False),
    sa.Column('notifications_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('daily_reminder_time', sa.Time(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("language ~ '^[a-z]{2}(-[A-Z]{2})?$'", name=op.f('ck_user_settings_language_format')),
    sa.CheckConstraint("theme IN ('light', 'dark', 'system')", name=op.f('ck_user_settings_theme_valid')),
    sa.CheckConstraint("time_format IN ('12h', '24h')", name=op.f('ck_user_settings_time_format_valid')),
    sa.CheckConstraint('week_start_day BETWEEN 1 AND 7', name=op.f('ck_user_settings_week_start_day_range')),
    sa.ForeignKeyConstraint(['user_id'], ['profiles.id'], name=op.f('fk_user_settings_user_id_profiles'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('user_id', name=op.f('pk_user_settings'))
    )
    op.create_table('assessment_answers',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('assessment_id', sa.UUID(), nullable=False),
    sa.Column('assessment_version_id', sa.UUID(), nullable=False),
    sa.Column('question_id', sa.UUID(), nullable=False),
    sa.Column('value_number', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('value_text', sa.Text(), nullable=True),
    sa.Column('value_options', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('num_nonnulls(value_number, value_text, value_options) = 1', name=op.f('ck_assessment_answers_exactly_one_value')),
    sa.ForeignKeyConstraint(['assessment_id', 'assessment_version_id'], ['assessments.id', 'assessments.assessment_version_id'], name='fk_assessment_answers_assessment_version', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['assessment_id', 'user_id'], ['assessments.id', 'assessments.user_id'], name='fk_assessment_answers_assessment_owner', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['question_id', 'assessment_version_id'], ['assessment_questions.id', 'assessment_questions.assessment_version_id'], name='fk_assessment_answers_question_in_version', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assessment_answers')),
    sa.UniqueConstraint('assessment_id', 'question_id', name='uq_assessment_answers_assessment_question')
    )
    op.create_index('ix_assessment_answers_question_id', 'assessment_answers', ['question_id'], unique=False)
    op.create_index('ix_assessment_answers_user_id', 'assessment_answers', ['user_id'], unique=False)
    op.create_table('habit_versions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('habit_id', sa.UUID(), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('goal_type', sa.String(length=10), nullable=False),
    sa.Column('target_value', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('unit', sa.String(length=32), nullable=True),
    sa.Column('frequency_type', sa.String(length=16), nullable=False),
    sa.Column('weekly_target_count', sa.SmallInteger(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(frequency_type = 'times_per_week' AND weekly_target_count IS NOT NULL AND weekly_target_count BETWEEN 1 AND 7) OR (frequency_type <> 'times_per_week' AND weekly_target_count IS NULL)", name=op.f('ck_habit_versions_weekly_target_shape')),
    sa.CheckConstraint("(goal_type = 'binary' AND target_value IS NULL AND unit IS NULL) OR (goal_type = 'quantity' AND target_value IS NOT NULL AND target_value > 0 AND unit IS NOT NULL) OR (goal_type = 'duration' AND target_value IS NOT NULL AND target_value > 0 AND unit = 'minutes')", name=op.f('ck_habit_versions_goal_shape')),
    sa.CheckConstraint("frequency_type IN ('daily', 'weekdays', 'times_per_week')", name=op.f('ck_habit_versions_frequency_type_valid')),
    sa.CheckConstraint("goal_type IN ('binary', 'quantity', 'duration')", name=op.f('ck_habit_versions_goal_type_valid')),
    sa.ForeignKeyConstraint(['habit_id'], ['habits.id'], name=op.f('fk_habit_versions_habit_id_habits'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_habit_versions')),
    sa.UniqueConstraint('habit_id', 'effective_from', name='uq_habit_versions_habit_from'),
    sa.UniqueConstraint('id', 'habit_id', name='uq_habit_versions_id_habit_id')
    )
    op.create_table('improvement_plans',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('assessment_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
    sa.Column('started_on', sa.Date(), nullable=False),
    sa.Column('ended_on', sa.Date(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'active') = (ended_on IS NULL)", name=op.f('ck_improvement_plans_ended_on_matches_status')),
    sa.CheckConstraint("status IN ('active', 'completed', 'archived')", name=op.f('ck_improvement_plans_status_valid')),
    sa.CheckConstraint('ended_on IS NULL OR ended_on >= started_on', name=op.f('ck_improvement_plans_ended_after_started')),
    sa.ForeignKeyConstraint(['assessment_id', 'user_id'], ['assessments.id', 'assessments.user_id'], name='fk_improvement_plans_assessment_owner', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['profiles.id'], name=op.f('fk_improvement_plans_user_id_profiles'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_improvement_plans')),
    sa.UniqueConstraint('assessment_id', name='uq_improvement_plans_assessment_id'),
    sa.UniqueConstraint('id', 'user_id', name='uq_improvement_plans_id_user_id')
    )
    op.create_index('ix_improvement_plans_user_id_status', 'improvement_plans', ['user_id', 'status'], unique=False)
    op.create_index('uq_improvement_plans_user_active', 'improvement_plans', ['user_id'], unique=True, postgresql_where=sa.text("status = 'active'"))
    op.create_table('tasks',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('priority', sa.String(length=10), server_default='medium', nullable=False),
    sa.Column('status', sa.String(length=12), server_default='todo', nullable=False),
    sa.Column('scheduled_date', sa.Date(), nullable=True),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'done') = (completed_at IS NOT NULL)", name=op.f('ck_tasks_completed_at_matches_status')),
    sa.CheckConstraint("priority IN ('low', 'medium', 'high')", name=op.f('ck_tasks_priority_valid')),
    sa.CheckConstraint("status IN ('todo', 'in_progress', 'done', 'cancelled')", name=op.f('ck_tasks_status_valid')),
    sa.CheckConstraint('due_date IS NULL OR scheduled_date IS NULL OR due_date >= scheduled_date', name=op.f('ck_tasks_due_after_scheduled')),
    sa.ForeignKeyConstraint(['project_id', 'user_id'], ['projects.id', 'projects.user_id'], name='fk_tasks_project_owner', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['profiles.id'], name=op.f('fk_tasks_user_id_profiles'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tasks')),
    sa.UniqueConstraint('id', 'user_id', name='uq_tasks_id_user_id')
    )
    op.create_index('ix_tasks_project_id', 'tasks', ['project_id'], unique=False)
    op.create_index('ix_tasks_user_id_scheduled_date', 'tasks', ['user_id', 'scheduled_date'], unique=False)
    op.create_index('ix_tasks_user_id_status', 'tasks', ['user_id', 'status'], unique=False)
    op.create_table('habit_logs',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('habit_id', sa.UUID(), nullable=False),
    sa.Column('habit_version_id', sa.UUID(), nullable=False),
    sa.Column('occurrence_date', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('value', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status <> 'skipped' OR value IS NULL", name=op.f('ck_habit_logs_skipped_has_no_value')),
    sa.CheckConstraint("status IN ('completed', 'partial', 'skipped')", name=op.f('ck_habit_logs_status_valid')),
    sa.CheckConstraint('value IS NULL OR value >= 0', name=op.f('ck_habit_logs_value_nonneg')),
    sa.ForeignKeyConstraint(['habit_id', 'user_id'], ['habits.id', 'habits.user_id'], name='fk_habit_logs_habit_owner', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['habit_version_id', 'habit_id'], ['habit_versions.id', 'habit_versions.habit_id'], name='fk_habit_logs_version_of_habit', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_habit_logs')),
    sa.UniqueConstraint('habit_id', 'occurrence_date', name='uq_habit_logs_habit_date'),
    sa.UniqueConstraint('id', 'user_id', name='uq_habit_logs_id_user_id')
    )
    op.create_index('ix_habit_logs_habit_version_id', 'habit_logs', ['habit_version_id'], unique=False)
    op.create_index('ix_habit_logs_user_id_occurrence_date', 'habit_logs', ['user_id', 'occurrence_date'], unique=False)
    op.create_table('habit_version_days',
    sa.Column('habit_version_id', sa.UUID(), nullable=False),
    sa.Column('weekday', sa.SmallInteger(), nullable=False),
    sa.CheckConstraint('weekday BETWEEN 1 AND 7', name=op.f('ck_habit_version_days_weekday_range')),
    sa.ForeignKeyConstraint(['habit_version_id'], ['habit_versions.id'], name=op.f('fk_habit_version_days_habit_version_id_habit_versions'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('habit_version_id', 'weekday', name=op.f('pk_habit_version_days'))
    )
    op.create_table('missions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('plan_id', sa.UUID(), nullable=False),
    sa.Column('habit_template_id', sa.UUID(), nullable=False),
    sa.Column('week_start', sa.Date(), nullable=False),
    sa.Column('weekly_target_count', sa.SmallInteger(), nullable=False),
    sa.Column('target_value', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('unit', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=12), server_default='active', nullable=False),
    sa.Column('adapted_from_mission_id', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('active', 'completed', 'abandoned', 'superseded')", name=op.f('ck_missions_status_valid')),
    sa.CheckConstraint('target_value IS NULL OR target_value > 0', name=op.f('ck_missions_target_value_positive')),
    sa.CheckConstraint('weekly_target_count BETWEEN 1 AND 7', name=op.f('ck_missions_weekly_target_range')),
    sa.ForeignKeyConstraint(['adapted_from_mission_id', 'user_id'], ['missions.id', 'missions.user_id'], name='fk_missions_adapted_from_owner', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['habit_template_id'], ['habit_templates.id'], name=op.f('fk_missions_habit_template_id_habit_templates'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['plan_id', 'user_id'], ['improvement_plans.id', 'improvement_plans.user_id'], name='fk_missions_plan_owner', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_missions')),
    sa.UniqueConstraint('id', 'user_id', name='uq_missions_id_user_id'),
    sa.UniqueConstraint('plan_id', 'habit_template_id', 'week_start', name='uq_missions_plan_template_week')
    )
    op.create_index('ix_missions_adapted_from_mission_id', 'missions', ['adapted_from_mission_id'], unique=False)
    op.create_index('ix_missions_habit_template_id', 'missions', ['habit_template_id'], unique=False)
    op.create_index('ix_missions_plan_id', 'missions', ['plan_id'], unique=False)
    op.create_index('ix_missions_user_id_status', 'missions', ['user_id', 'status'], unique=False)
    op.create_index('ix_missions_user_id_week_start', 'missions', ['user_id', 'week_start'], unique=False)
    op.create_table('task_checklist_items',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('task_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('is_done', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('position >= 0', name=op.f('ck_task_checklist_items_position_nonneg')),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_task_checklist_items_task_id_tasks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_task_checklist_items')),
    sa.UniqueConstraint('task_id', 'position', deferrable=True, initially='DEFERRED', name='uq_task_checklist_items_task_position')
    )
    op.create_table('weekly_reviews',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('plan_id', sa.UUID(), nullable=False),
    sa.Column('week_start', sa.Date(), nullable=False),
    sa.Column('perceived_difficulty', sa.SmallInteger(), nullable=False),
    sa.Column('energy_level', sa.SmallInteger(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('energy_level BETWEEN 1 AND 5', name=op.f('ck_weekly_reviews_energy_range')),
    sa.CheckConstraint('perceived_difficulty BETWEEN 1 AND 5', name=op.f('ck_weekly_reviews_difficulty_range')),
    sa.ForeignKeyConstraint(['plan_id', 'user_id'], ['improvement_plans.id', 'improvement_plans.user_id'], name='fk_weekly_reviews_plan_owner', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_weekly_reviews')),
    sa.UniqueConstraint('plan_id', 'week_start', name='uq_weekly_reviews_plan_week')
    )
    op.create_index('ix_weekly_reviews_user_id', 'weekly_reviews', ['user_id'], unique=False)
    op.create_table('mission_logs',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('mission_id', sa.UUID(), nullable=False),
    sa.Column('occurrence_date', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('value', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('perceived_difficulty', sa.SmallInteger(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status <> 'skipped' OR value IS NULL", name=op.f('ck_mission_logs_skipped_has_no_value')),
    sa.CheckConstraint("status IN ('completed', 'partial', 'skipped')", name=op.f('ck_mission_logs_status_valid')),
    sa.CheckConstraint('perceived_difficulty IS NULL OR perceived_difficulty BETWEEN 1 AND 5', name=op.f('ck_mission_logs_difficulty_range')),
    sa.CheckConstraint('value IS NULL OR value >= 0', name=op.f('ck_mission_logs_value_nonneg')),
    sa.ForeignKeyConstraint(['mission_id', 'user_id'], ['missions.id', 'missions.user_id'], name='fk_mission_logs_mission_owner', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_mission_logs')),
    sa.UniqueConstraint('id', 'user_id', name='uq_mission_logs_id_user_id'),
    sa.UniqueConstraint('mission_id', 'occurrence_date', name='uq_mission_logs_mission_date')
    )
    op.create_index('ix_mission_logs_user_id_occurrence_date', 'mission_logs', ['user_id', 'occurrence_date'], unique=False)
    op.create_table('xp_transactions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('xp_rule_id', sa.UUID(), nullable=False),
    sa.Column('event_type', sa.String(length=16), server_default='completion', nullable=False),
    sa.Column('habit_log_id', sa.UUID(), nullable=True),
    sa.Column('mission_log_id', sa.UUID(), nullable=True),
    sa.Column('task_id', sa.UUID(), nullable=True),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('award_date', sa.Date(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=120), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("event_type IN ('completion')", name=op.f('ck_xp_transactions_event_type_valid')),
    sa.CheckConstraint('amount >= 0', name=op.f('ck_xp_transactions_amount_nonneg')),
    sa.CheckConstraint('num_nonnulls(habit_log_id, mission_log_id, task_id) = 1', name=op.f('ck_xp_transactions_exactly_one_source')),
    sa.ForeignKeyConstraint(['habit_log_id', 'user_id'], ['habit_logs.id', 'habit_logs.user_id'], name='fk_xp_transactions_habit_log_owner', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['mission_log_id', 'user_id'], ['mission_logs.id', 'mission_logs.user_id'], name='fk_xp_transactions_mission_log_owner', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['task_id', 'user_id'], ['tasks.id', 'tasks.user_id'], name='fk_xp_transactions_task_owner', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['profiles.id'], name=op.f('fk_xp_transactions_user_id_profiles'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['xp_rule_id'], ['xp_rules.id'], name=op.f('fk_xp_transactions_xp_rule_id_xp_rules'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_xp_transactions')),
    sa.UniqueConstraint('habit_log_id', 'event_type', name='uq_xp_transactions_habit_log_event'),
    sa.UniqueConstraint('mission_log_id', 'event_type', name='uq_xp_transactions_mission_log_event'),
    sa.UniqueConstraint('task_id', 'event_type', name='uq_xp_transactions_task_event'),
    sa.UniqueConstraint('user_id', 'idempotency_key', name='uq_xp_transactions_user_key')
    )
    op.create_index('ix_xp_transactions_user_id_award_date', 'xp_transactions', ['user_id', 'award_date'], unique=False)
    op.create_index('ix_xp_transactions_user_id_created_at', 'xp_transactions', ['user_id', 'created_at'], unique=False)
    op.create_index('ix_xp_transactions_xp_rule_id', 'xp_transactions', ['xp_rule_id'], unique=False)
    for statement in TRIGGER_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROP_TRIGGER_STATEMENTS:
        op.execute(statement)
    op.drop_index('ix_xp_transactions_xp_rule_id', table_name='xp_transactions')
    op.drop_index('ix_xp_transactions_user_id_created_at', table_name='xp_transactions')
    op.drop_index('ix_xp_transactions_user_id_award_date', table_name='xp_transactions')
    op.drop_table('xp_transactions')
    op.drop_index('ix_mission_logs_user_id_occurrence_date', table_name='mission_logs')
    op.drop_table('mission_logs')
    op.drop_index('ix_weekly_reviews_user_id', table_name='weekly_reviews')
    op.drop_table('weekly_reviews')
    op.drop_table('task_checklist_items')
    op.drop_index('ix_missions_user_id_week_start', table_name='missions')
    op.drop_index('ix_missions_user_id_status', table_name='missions')
    op.drop_index('ix_missions_plan_id', table_name='missions')
    op.drop_index('ix_missions_habit_template_id', table_name='missions')
    op.drop_index('ix_missions_adapted_from_mission_id', table_name='missions')
    op.drop_table('missions')
    op.drop_table('habit_version_days')
    op.drop_index('ix_habit_logs_user_id_occurrence_date', table_name='habit_logs')
    op.drop_index('ix_habit_logs_habit_version_id', table_name='habit_logs')
    op.drop_table('habit_logs')
    op.drop_index('ix_tasks_user_id_status', table_name='tasks')
    op.drop_index('ix_tasks_user_id_scheduled_date', table_name='tasks')
    op.drop_index('ix_tasks_project_id', table_name='tasks')
    op.drop_table('tasks')
    op.drop_index('uq_improvement_plans_user_active', table_name='improvement_plans', postgresql_where=sa.text("status = 'active'"))
    op.drop_index('ix_improvement_plans_user_id_status', table_name='improvement_plans')
    op.drop_table('improvement_plans')
    op.drop_table('habit_versions')
    op.drop_index('ix_assessment_answers_user_id', table_name='assessment_answers')
    op.drop_index('ix_assessment_answers_question_id', table_name='assessment_answers')
    op.drop_table('assessment_answers')
    op.drop_table('user_settings')
    op.drop_table('user_game_profiles')
    op.drop_index('uq_projects_user_open_name', table_name='projects', postgresql_where=sa.text("status <> 'archived'"))
    op.drop_index('ix_projects_user_id_status', table_name='projects')
    op.drop_table('projects')
    op.drop_index('uq_habits_user_active_name', table_name='habits', postgresql_where=sa.text("status = 'active'"))
    op.drop_index('ix_habits_user_id_status', table_name='habits')
    op.drop_index('ix_habits_source_template_id', table_name='habits')
    op.drop_table('habits')
    op.drop_index('uq_assessments_user_in_progress', table_name='assessments', postgresql_where=sa.text("status = 'in_progress'"))
    op.drop_index('ix_assessments_user_id_started_at', table_name='assessments')
    op.drop_index('ix_assessments_assessment_version_id', table_name='assessments')
    op.drop_table('assessments')
    op.drop_table('assessment_questions')
    op.drop_table('xp_rules')
    op.drop_table('profiles')
    op.drop_index('ix_habit_templates_active_category', table_name='habit_templates')
    op.drop_table('habit_templates')
    op.drop_index('uq_assessment_versions_single_active', table_name='assessment_versions', postgresql_where=sa.text('is_active'))
    op.drop_table('assessment_versions')
