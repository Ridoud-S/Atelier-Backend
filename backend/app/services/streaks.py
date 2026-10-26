"""Activity streaks, computed from source rows (docs/data-model.md, section 5).

A day is "active" when the user COMPLETED at least one habit (user-created or system-generated)
or one mission that day. Tasks do not count. Each date counts once however many items were
completed, so habits and missions never double-count. `partial` and `skipped` do not count.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select, union
from sqlalchemy.orm import Session

from app.domain.enums import LogStatus
from app.models import HabitLog, MissionLog


@dataclass(frozen=True)
class Streak:
    current: int  # consecutive active days ending today (or yesterday, see below)
    longest: int
    last_active_date: date | None


def active_dates(session: Session, user_id: uuid.UUID) -> set[date]:
    habits = select(HabitLog.occurrence_date).where(
        HabitLog.user_id == user_id, HabitLog.status == LogStatus.COMPLETED.value
    )
    missions = select(MissionLog.occurrence_date).where(
        MissionLog.user_id == user_id, MissionLog.status == LogStatus.COMPLETED.value
    )
    return set(session.execute(union(habits, missions)).scalars())


def activity_streak(session: Session, user_id: uuid.UUID, today: date) -> Streak:
    """`today` is the user's LOCAL date. If today has no activity yet, the streak is not broken
    until the day ends: the current streak is then counted back from yesterday."""
    days = active_dates(session, user_id)
    if not days:
        return Streak(0, 0, None)

    cursor = today if today in days else today - timedelta(days=1)
    current = 0
    while cursor in days:
        current += 1
        cursor -= timedelta(days=1)

    longest = run = 0
    previous: date | None = None
    for day in sorted(days):
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day
    return Streak(current=current, longest=longest, last_active_date=max(days))
