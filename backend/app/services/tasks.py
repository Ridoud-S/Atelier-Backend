"""Task use cases: create and change status (docs/data-model.md, sections 3 and 6.4)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import TaskPriority, TaskStatus, XpSourceType
from app.domain.transitions import TASK_TRANSITIONS, assert_transition
from app.models import Project, Task, UserSettings
from app.services.errors import ValidationFailed
from app.services.ownership import get_owned
from app.services.timeutil import local_today
from app.services.xp import AwardResult, award_xp


def create_task(
    session: Session,
    user_id: uuid.UUID,
    *,
    title: str,
    priority: TaskPriority = TaskPriority.MEDIUM,
    project_id: uuid.UUID | None = None,
    description: str | None = None,
    scheduled_date: date | None = None,
    due_date: date | None = None,
) -> Task:
    if project_id is not None:
        # Raises NotFoundError (404) for another user's project, before touching the DB FK.
        get_owned(session, Project, project_id, user_id)
    if scheduled_date and due_date and due_date < scheduled_date:
        raise ValidationFailed("due_date cannot be before scheduled_date")
    task = Task(
        user_id=user_id,
        project_id=project_id,
        title=title,
        description=description,
        priority=priority.value,
        scheduled_date=scheduled_date,
        due_date=due_date,
    )
    session.add(task)
    session.flush()
    return task


@dataclass(frozen=True)
class TaskStatusResult:
    task: Task
    xp: AwardResult | None


def change_task_status(
    session: Session,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    target: TaskStatus,
    *,
    now: datetime | None = None,
) -> TaskStatusResult:
    """Move a task to `target`.

    - Requesting the state the task is already in is a successful no-op (safe API retries).
    - Completing grants XP exactly once in the task's whole life: reopening and completing
      again finds the existing transaction (same idempotency key / UNIQUE task_id).
    """
    instant = now or datetime.now(UTC)
    # Row lock: concurrent status changes of the same task are serialized.
    task = get_owned(session, Task, task_id, user_id, for_update=True)

    if task.status == target:
        xp = None
        if target == TaskStatus.DONE:
            xp = _award(session, task, instant)  # replay returns the existing transaction
        return TaskStatusResult(task, xp)

    assert_transition(TASK_TRANSITIONS, task.status, target, "task")
    task.status = target.value
    task.completed_at = instant if target == TaskStatus.DONE else None
    session.flush()

    xp = _award(session, task, instant) if target == TaskStatus.DONE else None
    return TaskStatusResult(task, xp)


def _award(session: Session, task: Task, instant: datetime) -> AwardResult:
    timezone = session.execute(
        select(UserSettings.timezone).where(UserSettings.user_id == task.user_id)
    ).scalar_one_or_none() or "UTC"
    return award_xp(
        session,
        user_id=task.user_id,
        source_type=XpSourceType.TASK,
        source_id=task.id,
        rule_code=f"task.completed.{task.priority}",
        award_date=local_today(timezone, instant),
    )
