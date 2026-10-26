"""Request/response models. Note: none of them accepts a `user_id`."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    FrequencyType,
    GoalType,
    HabitCategory,
    TaskPriority,
    TaskStatus,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a client-supplied user_id is rejected (422)


class MeOut(BaseModel):
    id: uuid.UUID
    timezone: str
    total_xp: int
    level: int


class HabitIn(_Strict):
    name: str = Field(min_length=1, max_length=120)
    category: HabitCategory
    start_date: date
    goal_type: GoalType = GoalType.BINARY
    target_value: Decimal | None = None
    unit: str | None = None
    frequency_type: FrequencyType = FrequencyType.DAILY
    weekly_target_count: int | None = None
    weekdays: list[int] = []
    description: str | None = None


class HabitOut(BaseModel):
    id: uuid.UUID
    name: str
    category: str
    status: str
    versions: int


class HabitLogIn(_Strict):
    occurrence_date: date
    value: Decimal | None = None
    skipped: bool = False


class XpOut(BaseModel):
    amount: int
    created: bool


class HabitLogOut(BaseModel):
    id: uuid.UUID
    status: str
    xp: XpOut | None


class TaskIn(_Strict):
    title: str = Field(min_length=1, max_length=200)
    priority: TaskPriority = TaskPriority.MEDIUM
    project_id: uuid.UUID | None = None
    description: str | None = None
    scheduled_date: date | None = None
    due_date: date | None = None


class TaskStatusIn(_Strict):
    status: TaskStatus


class TaskOut(BaseModel):
    id: uuid.UUID
    title: str
    status: str
    priority: str
    project_id: uuid.UUID | None
    xp: XpOut | None = None
