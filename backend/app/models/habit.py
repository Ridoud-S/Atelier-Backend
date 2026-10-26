"""Personal habits (module B): identity, versions, scheduled weekdays and daily logs."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, TimestampMixin, uuid_pk
from app.domain.enums import (
    FrequencyType,
    GoalType,
    HabitCategory,
    HabitOrigin,
    HabitStatus,
    LogStatus,
    sql_in,
)


def goal_constraints() -> tuple[CheckConstraint, ...]:
    """Shape of (goal_type, target_value, unit); shared by habit versions and templates."""
    return (
        CheckConstraint(sql_in("goal_type", GoalType), name="goal_type_valid"),
        CheckConstraint(
            # Explicit IS NOT NULL: a CHECK that evaluates to NULL is *accepted* by PostgreSQL.
            "(goal_type = 'binary' AND target_value IS NULL AND unit IS NULL)"
            " OR (goal_type = 'quantity' AND target_value IS NOT NULL AND target_value > 0"
            " AND unit IS NOT NULL)"
            " OR (goal_type = 'duration' AND target_value IS NOT NULL AND target_value > 0"
            " AND unit = 'minutes')",
            name="goal_shape",
        ),
    )


class HabitTemplate(Base, CreatedAtMixin):
    """System catalog entry. Immutable by convention: a change is a new `version` row."""

    __tablename__ = "habit_templates"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    goal_type: Mapped[str] = mapped_column(String(10), nullable=False)
    target_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    unit: Mapped[str | None] = mapped_column(String(32))
    default_weekly_target_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    difficulty: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default=text("true"))

    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_habit_templates_code_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(sql_in("category", HabitCategory), name="category_valid"),
        CheckConstraint("default_weekly_target_count BETWEEN 1 AND 7", name="weekly_target_range"),
        CheckConstraint("difficulty BETWEEN 1 AND 5", name="difficulty_range"),
        *goal_constraints(),
        Index("ix_habit_templates_active_category", "is_active", "category"),
    )


class Habit(Base, TimestampMixin):
    """Stable identity of a habit. Frequency and goal live in `HabitVersion`."""

    __tablename__ = "habits"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    origin: Mapped[str] = mapped_column(
        String(10), nullable=False, default=HabitOrigin.USER.value, server_default="user"
    )
    source_template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("habit_templates.id", ondelete="RESTRICT")
    )
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=HabitStatus.ACTIVE.value, server_default="active"
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    versions: Mapped[list[HabitVersion]] = relationship(
        back_populates="habit", order_by="HabitVersion.effective_from"
    )

    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_habits_id_user_id"),
        CheckConstraint(sql_in("category", HabitCategory), name="category_valid"),
        CheckConstraint(sql_in("origin", HabitOrigin), name="origin_valid"),
        CheckConstraint(sql_in("status", HabitStatus), name="status_valid"),
        CheckConstraint(
            "origin = 'system' OR source_template_id IS NULL", name="template_only_if_system"
        ),
        CheckConstraint(
            "(status = 'archived') = (archived_at IS NOT NULL)", name="archived_at_matches_status"
        ),
        Index("ix_habits_user_id_status", "user_id", "status"),
        Index("ix_habits_source_template_id", "source_template_id"),
        Index(
            "uq_habits_user_active_name",
            "user_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class HabitVersion(Base, TimestampMixin):
    """How a habit is interpreted from `effective_from` until the next version (exclusive)."""

    __tablename__ = "habit_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    habit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("habits.id", ondelete="RESTRICT"), nullable=False
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    goal_type: Mapped[str] = mapped_column(String(10), nullable=False)
    target_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    unit: Mapped[str | None] = mapped_column(String(32))
    frequency_type: Mapped[str] = mapped_column(String(16), nullable=False)
    weekly_target_count: Mapped[int | None] = mapped_column(SmallInteger)

    habit: Mapped[Habit] = relationship(back_populates="versions")
    days: Mapped[list[HabitVersionDay]] = relationship(
        back_populates="version", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("habit_id", "effective_from", name="uq_habit_versions_habit_from"),
        UniqueConstraint("id", "habit_id", name="uq_habit_versions_id_habit_id"),
        CheckConstraint(sql_in("frequency_type", FrequencyType), name="frequency_type_valid"),
        CheckConstraint(
            "(frequency_type = 'times_per_week' AND weekly_target_count IS NOT NULL"
            " AND weekly_target_count BETWEEN 1 AND 7)"
            " OR (frequency_type <> 'times_per_week' AND weekly_target_count IS NULL)",
            name="weekly_target_shape",
        ),
        *goal_constraints(),
    )


class HabitVersionDay(Base):
    """ISO weekdays (1=Mon..7=Sun) of a `weekdays` version. Dependent part: CASCADE."""

    __tablename__ = "habit_version_days"

    habit_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("habit_versions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, primary_key=True)

    version: Mapped[HabitVersion] = relationship(back_populates="days")

    __table_args__ = (CheckConstraint("weekday BETWEEN 1 AND 7", name="weekday_range"),)


class HabitLog(Base, TimestampMixin):
    """At most one row per habit and local date."""

    __tablename__ = "habit_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    habit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    habit_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    occurrence_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    __table_args__ = (
        ForeignKeyConstraint(
            ["habit_id", "user_id"],
            ["habits.id", "habits.user_id"],
            name="fk_habit_logs_habit_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["habit_version_id", "habit_id"],
            ["habit_versions.id", "habit_versions.habit_id"],
            name="fk_habit_logs_version_of_habit",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("habit_id", "occurrence_date", name="uq_habit_logs_habit_date"),
        UniqueConstraint("id", "user_id", name="uq_habit_logs_id_user_id"),
        CheckConstraint(sql_in("status", LogStatus), name="status_valid"),
        CheckConstraint("value IS NULL OR value >= 0", name="value_nonneg"),
        CheckConstraint("status <> 'skipped' OR value IS NULL", name="skipped_has_no_value"),
        Index("ix_habit_logs_user_id_occurrence_date", "user_id", "occurrence_date"),
        Index("ix_habit_logs_habit_version_id", "habit_version_id"),
    )
