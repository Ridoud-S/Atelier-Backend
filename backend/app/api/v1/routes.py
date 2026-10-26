"""Minimal v1 vertical slice. Every handler scopes by the *token's* user id."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select

from app.api.deps import RequestContext, get_context
from app.api.v1 import schemas
from app.domain.enums import HabitCategory, HabitStatus
from app.models import Habit, HabitVersion, UserGameProfile, UserSettings
from app.services import habits as habit_service
from app.services import tasks as task_service
from app.services.ownership import get_owned
from app.services.xp import AwardResult, level_for_xp

router = APIRouter()


def _xp(result: AwardResult | None) -> schemas.XpOut | None:
    if result is None:
        return None
    return schemas.XpOut(amount=result.transaction.amount, created=result.created)


@router.get("/me", response_model=schemas.MeOut)
def read_me(ctx: RequestContext = Depends(get_context)):
    timezone = ctx.session.execute(
        select(UserSettings.timezone).where(UserSettings.user_id == ctx.user_id)
    ).scalar_one()
    total = ctx.session.execute(
        select(UserGameProfile.total_xp).where(UserGameProfile.user_id == ctx.user_id)
    ).scalar_one()
    return schemas.MeOut(id=ctx.user_id, timezone=timezone, total_xp=total, level=level_for_xp(total))


@router.post("/habits", response_model=schemas.HabitOut, status_code=status.HTTP_201_CREATED)
def create_habit(body: schemas.HabitIn, ctx: RequestContext = Depends(get_context)):
    habit = habit_service.create_habit(
        ctx.session,
        ctx.user_id,
        name=body.name,
        category=HabitCategory(body.category),
        start_date=body.start_date,
        description=body.description,
        spec=habit_service.HabitSpec(
            goal_type=body.goal_type,
            target_value=body.target_value,
            unit=body.unit,
            frequency_type=body.frequency_type,
            weekly_target_count=body.weekly_target_count,
            weekdays=tuple(body.weekdays),
        ),
    )
    ctx.session.commit()
    return schemas.HabitOut(
        id=habit.id, name=habit.name, category=habit.category, status=habit.status, versions=1
    )


@router.get("/habits/{habit_id}", response_model=schemas.HabitOut)
def read_habit(habit_id: uuid.UUID, ctx: RequestContext = Depends(get_context)):
    habit = get_owned(ctx.session, Habit, habit_id, ctx.user_id)
    versions = ctx.session.execute(
        select(func.count()).select_from(HabitVersion).where(HabitVersion.habit_id == habit.id)
    ).scalar_one()
    return schemas.HabitOut(
        id=habit.id, name=habit.name, category=habit.category, status=habit.status, versions=versions
    )


@router.post("/habits/{habit_id}/archive", response_model=schemas.HabitOut)
def archive_habit(habit_id: uuid.UUID, ctx: RequestContext = Depends(get_context)):
    from datetime import UTC, datetime

    habit = habit_service.set_habit_status(
        ctx.session, ctx.user_id, habit_id, HabitStatus.ARCHIVED, datetime.now(UTC)
    )
    ctx.session.commit()
    return schemas.HabitOut(
        id=habit.id, name=habit.name, category=habit.category, status=habit.status, versions=0
    )


@router.post("/habits/{habit_id}/logs", response_model=schemas.HabitLogOut)
def log_habit(habit_id: uuid.UUID, body: schemas.HabitLogIn, ctx: RequestContext = Depends(get_context)):
    result = habit_service.log_habit(
        ctx.session,
        ctx.user_id,
        habit_id,
        body.occurrence_date,
        value=body.value,
        skipped=body.skipped,
    )
    ctx.session.commit()
    return schemas.HabitLogOut(id=result.log.id, status=result.log.status, xp=_xp(result.xp))


@router.post("/tasks", response_model=schemas.TaskOut, status_code=status.HTTP_201_CREATED)
def create_task(body: schemas.TaskIn, ctx: RequestContext = Depends(get_context)):
    task = task_service.create_task(ctx.session, ctx.user_id, **body.model_dump())
    ctx.session.commit()
    return schemas.TaskOut(
        id=task.id, title=task.title, status=task.status, priority=task.priority, project_id=task.project_id
    )


@router.get("/tasks/{task_id}", response_model=schemas.TaskOut)
def read_task(task_id: uuid.UUID, ctx: RequestContext = Depends(get_context)):
    from app.models import Task

    task = get_owned(ctx.session, Task, task_id, ctx.user_id)
    return schemas.TaskOut(
        id=task.id, title=task.title, status=task.status, priority=task.priority, project_id=task.project_id
    )


@router.post("/tasks/{task_id}/status", response_model=schemas.TaskOut)
def change_task_status(
    task_id: uuid.UUID, body: schemas.TaskStatusIn, ctx: RequestContext = Depends(get_context)
):
    result = task_service.change_task_status(ctx.session, ctx.user_id, task_id, body.status)
    ctx.session.commit()
    task = result.task
    return schemas.TaskOut(
        id=task.id,
        title=task.title,
        status=task.status,
        priority=task.priority,
        project_id=task.project_id,
        xp=_xp(result.xp),
    )
