"""Weekly progress for habits (docs/data-model.md, section 5). Computed from source rows."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import FrequencyType, LogStatus
from app.models import Habit, HabitLog, HabitVersion, HabitVersionDay
from app.services.ownership import get_owned


@dataclass(frozen=True)
class WeeklyProgress:
    week_start: date
    week_end: date
    eligible_days: int  # days of the week on which the habit existed
    target: int  # completions expected this week (0 => nothing expected)
    completed: int  # valid completions on distinct local dates that count toward the target
    extra: int  # completions beyond the target, plus completions on unscheduled days
    progress_ratio: float | None  # completed / target, NOT capped (may exceed 1.0)
    compliance_pct: float | None  # min(progress_ratio, 1) * 100, capped at 100


def weekly_progress(
    session: Session, user_id: uuid.UUID, habit_id: uuid.UUID, week_start: date
) -> WeeklyProgress | None:
    """Progress of one habit in the 7-day window starting at `week_start`.

    Returns None when the habit did not exist on any day of that week.
    """
    habit = get_owned(session, Habit, habit_id, user_id)
    versions = (
        session.execute(
            select(HabitVersion)
            .where(HabitVersion.habit_id == habit.id)
            .order_by(HabitVersion.effective_from)
        )
        .scalars()
        .all()
    )
    week_end = week_start + timedelta(days=6)
    if not versions:
        return None

    first_from = versions[0].effective_from
    eligible = [
        d
        for d in (week_start + timedelta(days=i) for i in range(7))
        if d >= first_from and (habit.end_date is None or d <= habit.end_date)
    ]
    if not eligible:
        return None

    scheduled_days: dict[uuid.UUID, set[int]] = {}
    for row in session.execute(
        select(HabitVersionDay).where(HabitVersionDay.habit_version_id.in_([v.id for v in versions]))
    ).scalars():
        scheduled_days.setdefault(row.habit_version_id, set()).add(row.weekday)

    def version_on(day: date) -> HabitVersion:
        return max((v for v in versions if v.effective_from <= day), key=lambda v: v.effective_from)

    def is_scheduled(version: HabitVersion, day: date) -> bool:
        if version.frequency_type == FrequencyType.WEEKDAYS:
            return day.isoweekday() in scheduled_days.get(version.id, set())
        return True  # daily and times_per_week accept any day

    expected = Fraction(0)
    for day in eligible:
        v = version_on(day)
        if v.frequency_type == FrequencyType.DAILY:
            expected += 1
        elif v.frequency_type == FrequencyType.WEEKDAYS:
            expected += 1 if is_scheduled(v, day) else 0
        else:  # times_per_week: spread evenly over the 7 days
            expected += Fraction(v.weekly_target_count, 7)

    target = 0 if expected == 0 else min(len(eligible), max(1, math.ceil(expected)))

    completed_dates = session.execute(
        select(HabitLog.occurrence_date).where(
            HabitLog.habit_id == habit.id,
            HabitLog.status == LogStatus.COMPLETED.value,
            HabitLog.occurrence_date.in_(eligible),
        )
    ).scalars()
    counted = unscheduled = 0
    for day in completed_dates:  # one row per date by UNIQUE => distinct local dates
        if is_scheduled(version_on(day), day):
            counted += 1
        else:
            unscheduled += 1

    if target == 0:
        ratio = pct = None
    else:
        ratio = counted / target
        pct = round(min(ratio, 1.0) * 100, 2)

    return WeeklyProgress(
        week_start=week_start,
        week_end=week_end,
        eligible_days=len(eligible),
        target=target,
        completed=counted,
        extra=max(counted - target, 0) + unscheduled,
        progress_ratio=ratio,
        compliance_pct=pct,
    )
