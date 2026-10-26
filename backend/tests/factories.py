"""Small builders so tests read as scenarios."""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TypeVar

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.domain.enums import HabitCategory, TaskPriority
from app.models import (
    Assessment,
    AssessmentVersion,
    HabitTemplate,
    ImprovementPlan,
    Mission,
    Project,
    UserSettings,
)
from app.services import habits as habit_service
from app.services import tasks as task_service
from app.services.habits import HabitSpec
from app.services.provisioning import ensure_profile

T = TypeVar("T")

# 2026-10-25 is a Sunday; its ISO week (Mon 19 .. Sun 25) is used throughout the suite.
NOW = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
TODAY = NOW.date()
MONDAY = date(2026, 10, 19)


def make_user(session: Session, *, timezone: str = "UTC", week_start_day: int = 1) -> uuid.UUID:
    uid = uuid.uuid4()
    ensure_profile(session, uid, display_name="Test user")
    session.execute(
        update(UserSettings)
        .where(UserSettings.user_id == uid)
        .values(timezone=timezone, week_start_day=week_start_day)
    )
    session.commit()
    return uid


def make_habit(
    session: Session,
    user_id: uuid.UUID,
    *,
    name: str = "Read",
    start: date = date(2026, 10, 1),
    spec: HabitSpec | None = None,
    category: HabitCategory = HabitCategory.FOCUS,
    end_date: date | None = None,
):
    habit = habit_service.create_habit(
        session,
        user_id,
        name=name,
        category=category,
        start_date=start,
        spec=spec or HabitSpec(),
        end_date=end_date,
    )
    session.commit()
    return habit


def make_task(session: Session, user_id: uuid.UUID, *, title: str = "Task", priority=TaskPriority.MEDIUM, **kw):
    task = task_service.create_task(session, user_id, title=title, priority=priority, **kw)
    session.commit()
    return task


def make_project(session: Session, user_id: uuid.UUID, name: str = "Project") -> Project:
    project = Project(user_id=user_id, name=name)
    session.add(project)
    session.commit()
    return project


def make_plan(session: Session, user_id: uuid.UUID) -> ImprovementPlan:
    """Needs `reference_data` (the questionnaire version)."""
    version = session.execute(select(AssessmentVersion).where(AssessmentVersion.version == 1)).scalar_one()
    assessment = Assessment(user_id=user_id, assessment_version_id=version.id, status="in_progress")
    session.add(assessment)
    session.flush()
    plan = ImprovementPlan(user_id=user_id, assessment_id=assessment.id, started_on=MONDAY)
    session.add(plan)
    session.commit()
    return plan


def make_mission(
    session: Session,
    user_id: uuid.UUID,
    plan: ImprovementPlan,
    *,
    week_start: date = MONDAY,
    template_code: str = "drink_water",
    weekly_target_count: int = 5,
) -> Mission:
    template = session.execute(select(HabitTemplate).where(HabitTemplate.code == template_code)).scalar_one()
    mission = Mission(
        user_id=user_id,
        plan_id=plan.id,
        habit_template_id=template.id,
        week_start=week_start,
        weekly_target_count=weekly_target_count,
        target_value=template.target_value,
        unit=template.unit,
    )
    session.add(mission)
    session.commit()
    return mission


def run_concurrently(
    factory: sessionmaker[Session], work: Callable[[Session, int], T], workers: int
) -> list[T]:
    """Run `work(session, index)` in `workers` threads at the same instant.

    Each thread has its own session/connection and commits on success, like separate API
    requests would. A barrier makes them start together to maximize contention.
    """
    barrier = threading.Barrier(workers)

    def target(index: int) -> T:
        with factory() as s:
            barrier.wait(timeout=10)
            try:
                result = work(s, index)
                s.commit()
                return result
            except Exception:
                s.rollback()
                raise

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(target, i) for i in range(workers)]
        return [f.result(timeout=60) for f in futures]


DECIMAL = Decimal
