"""Habit use cases: create, version, archive and log (docs/data-model.md, sections 3-5)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.domain.enums import (
    FrequencyType,
    GoalType,
    HabitCategory,
    HabitOrigin,
    HabitStatus,
    LogStatus,
    XpSourceType,
)
from app.domain.transitions import HABIT_TRANSITIONS, assert_transition
from app.models import Habit, HabitLog, HabitVersion, HabitVersionDay, UserSettings
from app.services.errors import ConflictError, NotFoundError, ValidationFailed
from app.services.ownership import get_owned
from app.core.config import get_settings
from app.services.timeutil import assert_loggable_date, local_today
from app.services.xp import AwardResult, award_xp

HABIT_XP_RULE = "habit.completed"


@dataclass(frozen=True)
class HabitSpec:
    """Frequency + goal of one habit version."""

    goal_type: GoalType = GoalType.BINARY
    target_value: Decimal | None = None
    unit: str | None = None
    frequency_type: FrequencyType = FrequencyType.DAILY
    weekly_target_count: int | None = None
    weekdays: tuple[int, ...] = field(default_factory=tuple)  # ISO 1..7, for `weekdays`

    def __post_init__(self) -> None:
        # Accept plain strings ("quantity") and coerce them; an unknown value raises ValueError.
        object.__setattr__(self, "goal_type", GoalType(self.goal_type))
        object.__setattr__(self, "frequency_type", FrequencyType(self.frequency_type))
        object.__setattr__(self, "weekdays", tuple(self.weekdays))


def validate_spec(spec: HabitSpec) -> None:
    """Backend validation that mirrors (and explains) the database CHECKs."""
    if spec.goal_type == GoalType.BINARY:
        if spec.target_value is not None or spec.unit is not None:
            raise ValidationFailed("binary habits have no target_value or unit")
    elif spec.target_value is None or spec.target_value <= 0:
        raise ValidationFailed("quantity/duration habits need target_value > 0")
    elif spec.goal_type == GoalType.QUANTITY and not spec.unit:
        raise ValidationFailed("quantity habits need a unit")
    elif spec.goal_type == GoalType.DURATION and spec.unit != "minutes":
        raise ValidationFailed("duration habits use unit 'minutes'")

    if spec.frequency_type == FrequencyType.TIMES_PER_WEEK:
        if spec.weekly_target_count is None or not 1 <= spec.weekly_target_count <= 7:
            raise ValidationFailed("times_per_week needs weekly_target_count between 1 and 7")
    elif spec.weekly_target_count is not None:
        raise ValidationFailed("weekly_target_count only applies to times_per_week")

    if spec.frequency_type == FrequencyType.WEEKDAYS:
        if not spec.weekdays or any(not 1 <= d <= 7 for d in spec.weekdays):
            raise ValidationFailed("weekdays needs at least one ISO weekday (1-7)")
        if len(set(spec.weekdays)) != len(spec.weekdays):
            raise ValidationFailed("weekdays must not repeat")
    elif spec.weekdays:
        raise ValidationFailed("weekdays only applies to the 'weekdays' frequency")


def _new_version(habit_id: uuid.UUID, effective_from: date, spec: HabitSpec) -> HabitVersion:
    version = HabitVersion(
        habit_id=habit_id,
        effective_from=effective_from,
        goal_type=spec.goal_type.value,
        target_value=spec.target_value,
        unit=spec.unit,
        frequency_type=spec.frequency_type.value,
        weekly_target_count=spec.weekly_target_count,
    )
    version.days = [HabitVersionDay(weekday=d) for d in spec.weekdays]
    return version


def create_habit(
    session: Session,
    user_id: uuid.UUID,
    *,
    name: str,
    category: HabitCategory,
    start_date: date,
    spec: HabitSpec,
    description: str | None = None,
    end_date: date | None = None,
    origin: HabitOrigin = HabitOrigin.USER,
    source_template_id: uuid.UUID | None = None,
) -> Habit:
    """Create the habit and its first version in the same transaction."""
    validate_spec(spec)
    if end_date is not None and end_date < start_date:
        raise ValidationFailed("end_date cannot be before the start date")
    habit = Habit(
        user_id=user_id,
        name=name,
        description=description,
        category=category.value,
        origin=origin.value,
        source_template_id=source_template_id,
        end_date=end_date,
    )
    session.add(habit)
    session.flush()
    session.add(_new_version(habit.id, start_date, spec))
    session.flush()
    return habit


def add_version(
    session: Session,
    user_id: uuid.UUID,
    habit_id: uuid.UUID,
    *,
    effective_from: date,
    spec: HabitSpec,
) -> HabitVersion:
    """Add a version. A future date is fine; a date on/before the last logged day is rejected
    by the database (it would reinterpret history)."""
    validate_spec(spec)
    habit = get_owned(session, Habit, habit_id, user_id)
    if habit.status == HabitStatus.ARCHIVED:
        raise ConflictError("archived habits cannot get new versions")
    version = _new_version(habit.id, effective_from, spec)
    session.add(version)
    session.flush()
    return version


def set_habit_status(
    session: Session, user_id: uuid.UUID, habit_id: uuid.UUID, target: HabitStatus, now: datetime
) -> Habit:
    habit = get_owned(session, Habit, habit_id, user_id, for_update=True)
    if habit.status == target:
        return habit
    assert_transition(HABIT_TRANSITIONS, habit.status, target, "habit")
    habit.status = target.value
    habit.archived_at = now if target == HabitStatus.ARCHIVED else None
    session.flush()
    return habit


def applicable_version(session: Session, habit_id: uuid.UUID, on_date: date) -> HabitVersion | None:
    """The version in force on `on_date`: the latest one with effective_from <= on_date."""
    return session.execute(
        select(HabitVersion)
        .where(HabitVersion.habit_id == habit_id, HabitVersion.effective_from <= on_date)
        .order_by(HabitVersion.effective_from.desc())
        .limit(1)
    ).scalar_one_or_none()


@dataclass(frozen=True)
class HabitLogResult:
    log: HabitLog
    xp: AwardResult | None  # None when the log did not complete the habit


def _decide_status(version: HabitVersion, value: Decimal | None, skipped: bool) -> tuple[LogStatus, Decimal | None]:
    if skipped:
        return LogStatus.SKIPPED, None
    if version.goal_type == GoalType.BINARY:
        return LogStatus.COMPLETED, None
    if value is None or value < 0:
        raise ValidationFailed("quantity/duration habits need a non-negative value")
    status = LogStatus.COMPLETED if value >= version.target_value else LogStatus.PARTIAL
    return status, value


def log_habit(
    session: Session,
    user_id: uuid.UUID,
    habit_id: uuid.UUID,
    occurrence_date: date,
    *,
    value: Decimal | None = None,
    skipped: bool = False,
    now: datetime | None = None,
    max_backfill_days: int | None = None,
) -> HabitLogResult:
    """Record (or correct) the occurrence of a habit on a local date.

    - One row per (habit, date): repeating the call updates it; it never duplicates.
    - Completing grants XP once; replays, corrections and re-completions do not repeat it.
    """
    habit = get_owned(session, Habit, habit_id, user_id)
    if habit.status == HabitStatus.ARCHIVED:
        raise ConflictError("archived habits cannot be logged")

    timezone = session.execute(
        select(UserSettings.timezone).where(UserSettings.user_id == user_id)
    ).scalar_one_or_none() or "UTC"
    today = local_today(timezone, now)
    limit = get_settings().max_backfill_days if max_backfill_days is None else max_backfill_days
    assert_loggable_date(occurrence_date, today, limit)

    version = applicable_version(session, habit.id, occurrence_date)
    if version is None:
        raise ValidationFailed("the habit has no version in force on that date")
    status, stored_value = _decide_status(version, value, skipped)

    # Atomic upsert: safe under concurrent identical requests (no check-then-insert race).
    stmt = (
        pg_insert(HabitLog)
        .values(
            user_id=user_id,
            habit_id=habit.id,
            habit_version_id=version.id,
            occurrence_date=occurrence_date,
            status=status.value,
            value=stored_value,
        )
        .on_conflict_do_update(
            constraint="uq_habit_logs_habit_date",
            set_={"status": status.value, "value": stored_value, "updated_at": func.now()},
        )
        .returning(HabitLog.id)
    )
    log_id = session.execute(stmt).scalar_one()
    log = session.execute(
        select(HabitLog).where(HabitLog.id == log_id).execution_options(populate_existing=True)
    ).scalar_one()

    xp = None
    if status == LogStatus.COMPLETED:
        xp = award_xp(
            session,
            user_id=user_id,
            source_type=XpSourceType.HABIT_LOG,
            source_id=log.id,
            rule_code=HABIT_XP_RULE,
            award_date=today,
        )
    return HabitLogResult(log=log, xp=xp)


__all__ = [
    "HabitSpec",
    "HabitLogResult",
    "NotFoundError",
    "add_version",
    "applicable_version",
    "create_habit",
    "log_habit",
    "set_habit_status",
    "validate_spec",
]
