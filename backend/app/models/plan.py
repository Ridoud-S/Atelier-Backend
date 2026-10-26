"""Improvement plans, weekly missions, mission logs and weekly reviews (module D)."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid_pk
from app.domain.enums import LogStatus, MissionStatus, PlanStatus, sql_in


class ImprovementPlan(Base, TimestampMixin):
    __tablename__ = "improvement_plans"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=PlanStatus.ACTIVE.value, server_default="active"
    )
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    ended_on: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        ForeignKeyConstraint(
            ["assessment_id", "user_id"],
            ["assessments.id", "assessments.user_id"],
            name="fk_improvement_plans_assessment_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("assessment_id", name="uq_improvement_plans_assessment_id"),
        UniqueConstraint("id", "user_id", name="uq_improvement_plans_id_user_id"),
        CheckConstraint(sql_in("status", PlanStatus), name="status_valid"),
        CheckConstraint("(status = 'active') = (ended_on IS NULL)", name="ended_on_matches_status"),
        CheckConstraint("ended_on IS NULL OR ended_on >= started_on", name="ended_after_started"),
        Index("ix_improvement_plans_user_id_status", "user_id", "status"),
        Index(
            "uq_improvement_plans_user_active",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class Mission(Base, TimestampMixin):
    """A habit template assigned for ONE week of a plan. Adaptation = a new mission next week."""

    __tablename__ = "missions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    habit_template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("habit_templates.id", ondelete="RESTRICT"), nullable=False
    )
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    weekly_target_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    target_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    unit: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=MissionStatus.ACTIVE.value, server_default="active"
    )
    adapted_from_mission_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "user_id"],
            ["improvement_plans.id", "improvement_plans.user_id"],
            name="fk_missions_plan_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["adapted_from_mission_id", "user_id"],
            ["missions.id", "missions.user_id"],
            name="fk_missions_adapted_from_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("plan_id", "habit_template_id", "week_start", name="uq_missions_plan_template_week"),
        UniqueConstraint("id", "user_id", name="uq_missions_id_user_id"),
        CheckConstraint(sql_in("status", MissionStatus), name="status_valid"),
        CheckConstraint("weekly_target_count BETWEEN 1 AND 7", name="weekly_target_range"),
        CheckConstraint("target_value IS NULL OR target_value > 0", name="target_value_positive"),
        Index("ix_missions_user_id_week_start", "user_id", "week_start"),
        Index("ix_missions_user_id_status", "user_id", "status"),
        Index("ix_missions_plan_id", "plan_id"),
        Index("ix_missions_habit_template_id", "habit_template_id"),
        Index("ix_missions_adapted_from_mission_id", "adapted_from_mission_id"),
    )


class MissionLog(Base, TimestampMixin):
    __tablename__ = "mission_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mission_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    occurrence_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    perceived_difficulty: Mapped[int | None] = mapped_column(SmallInteger)

    __table_args__ = (
        ForeignKeyConstraint(
            ["mission_id", "user_id"],
            ["missions.id", "missions.user_id"],
            name="fk_mission_logs_mission_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("mission_id", "occurrence_date", name="uq_mission_logs_mission_date"),
        UniqueConstraint("id", "user_id", name="uq_mission_logs_id_user_id"),
        CheckConstraint(sql_in("status", LogStatus), name="status_valid"),
        CheckConstraint("value IS NULL OR value >= 0", name="value_nonneg"),
        CheckConstraint("status <> 'skipped' OR value IS NULL", name="skipped_has_no_value"),
        CheckConstraint(
            "perceived_difficulty IS NULL OR perceived_difficulty BETWEEN 1 AND 5",
            name="difficulty_range",
        ),
        Index("ix_mission_logs_user_id_occurrence_date", "user_id", "occurrence_date"),
    )


class WeeklyReview(Base, TimestampMixin):
    __tablename__ = "weekly_reviews"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    perceived_difficulty: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    energy_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "user_id"],
            ["improvement_plans.id", "improvement_plans.user_id"],
            name="fk_weekly_reviews_plan_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("plan_id", "week_start", name="uq_weekly_reviews_plan_week"),
        CheckConstraint("perceived_difficulty BETWEEN 1 AND 5", name="difficulty_range"),
        CheckConstraint("energy_level BETWEEN 1 AND 5", name="energy_range"),
        Index("ix_weekly_reviews_user_id", "user_id"),
    )
