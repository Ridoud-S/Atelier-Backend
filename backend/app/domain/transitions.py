"""Allowed status transitions. The database guarantees the *values* (CHECK constraints);
the transitions are validated here and covered by tests (see docs/data-model.md, section 3)."""
from __future__ import annotations

from collections.abc import Mapping

from app.domain.enums import (
    AssessmentStatus,
    HabitStatus,
    MissionStatus,
    PlanStatus,
    ProjectStatus,
    TaskStatus,
)
from app.services.errors import InvalidTransitionError

Table = Mapping[str, frozenset[str]]


def _t(**kwargs: set[str]) -> Table:
    return {k: frozenset(v) for k, v in kwargs.items()}


HABIT_TRANSITIONS: Table = _t(
    **{HabitStatus.ACTIVE: {HabitStatus.ARCHIVED}, HabitStatus.ARCHIVED: {HabitStatus.ACTIVE}}
)
PROJECT_TRANSITIONS: Table = _t(
    **{
        ProjectStatus.ACTIVE: {ProjectStatus.COMPLETED, ProjectStatus.ARCHIVED},
        ProjectStatus.COMPLETED: {ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED},
        ProjectStatus.ARCHIVED: {ProjectStatus.ACTIVE},
    }
)
TASK_TRANSITIONS: Table = _t(
    **{
        TaskStatus.TODO: {TaskStatus.IN_PROGRESS, TaskStatus.DONE, TaskStatus.CANCELLED},
        TaskStatus.IN_PROGRESS: {TaskStatus.TODO, TaskStatus.DONE, TaskStatus.CANCELLED},
        TaskStatus.DONE: {TaskStatus.TODO, TaskStatus.IN_PROGRESS},
        TaskStatus.CANCELLED: {TaskStatus.TODO},
    }
)
ASSESSMENT_TRANSITIONS: Table = _t(
    **{
        AssessmentStatus.IN_PROGRESS: {AssessmentStatus.COMPLETED, AssessmentStatus.ABANDONED},
        AssessmentStatus.COMPLETED: set(),
        AssessmentStatus.ABANDONED: set(),
    }
)
PLAN_TRANSITIONS: Table = _t(
    **{
        PlanStatus.ACTIVE: {PlanStatus.COMPLETED, PlanStatus.ARCHIVED},
        PlanStatus.COMPLETED: {PlanStatus.ARCHIVED},
        PlanStatus.ARCHIVED: set(),
    }
)
MISSION_TRANSITIONS: Table = _t(
    **{
        MissionStatus.ACTIVE: {
            MissionStatus.COMPLETED,
            MissionStatus.ABANDONED,
            MissionStatus.SUPERSEDED,
        },
        MissionStatus.COMPLETED: set(),
        MissionStatus.ABANDONED: set(),
        MissionStatus.SUPERSEDED: set(),
    }
)


def can_transition(table: Table, current: str, target: str) -> bool:
    return str(target) in table.get(str(current), frozenset())


def assert_transition(table: Table, current: str, target: str, entity: str = "entity") -> None:
    if not can_transition(table, current, target):
        raise InvalidTransitionError(f"{entity}: cannot go from '{current}' to '{target}'")
