"""Valid values for every constrained column.

The database enforces them with CHECK constraints built from these enums, so the
Python side and the SQL side cannot drift apart.
"""
from __future__ import annotations

from enum import StrEnum


class HabitCategory(StrEnum):
    SLEEP = "sleep"
    HYDRATION = "hydration"
    NUTRITION = "nutrition"
    ACTIVITY = "activity"
    FOCUS = "focus"
    WELLBEING = "wellbeing"
    OTHER = "other"


class QuestionDomain(StrEnum):
    SLEEP = "sleep"
    HYDRATION = "hydration"
    NUTRITION = "nutrition"
    ACTIVITY = "activity"
    FOCUS = "focus"
    WELLBEING = "wellbeing"
    GOALS = "goals"
    AVAILABILITY = "availability"


class AnswerType(StrEnum):
    SCALE = "scale"
    NUMBER = "number"
    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    TEXT = "text"


class GoalType(StrEnum):
    BINARY = "binary"
    QUANTITY = "quantity"
    DURATION = "duration"


class FrequencyType(StrEnum):
    DAILY = "daily"
    WEEKDAYS = "weekdays"
    TIMES_PER_WEEK = "times_per_week"


class HabitOrigin(StrEnum):
    USER = "user"
    SYSTEM = "system"


class HabitStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class LogStatus(StrEnum):
    """Status of a habit log or mission log."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    SKIPPED = "skipped"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AssessmentStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class PlanStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class MissionStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"


class XpSourceType(StrEnum):
    HABIT_LOG = "habit_log"
    MISSION_LOG = "mission_log"
    TASK = "task"


class XpEventType(StrEnum):
    COMPLETION = "completion"


class Theme(StrEnum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class TimeFormat(StrEnum):
    H12 = "12h"
    H24 = "24h"


def sql_in(column: str, enum_cls: type[StrEnum]) -> str:
    """Build `column IN ('a', 'b', ...)` for a CHECK constraint."""
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({values})"
