"""Import every model so `Base.metadata` is complete (Alembic autogenerate, tests)."""
from app.models.assessment import (
    Assessment,
    AssessmentAnswer,
    AssessmentQuestion,
    AssessmentVersion,
)
from app.models.gamification import XpRule, XpTransaction
from app.models.habit import Habit, HabitLog, HabitTemplate, HabitVersion, HabitVersionDay
from app.models.plan import ImprovementPlan, Mission, MissionLog, WeeklyReview
from app.models.profile import Profile, UserGameProfile, UserSettings
from app.models.task import Project, Task, TaskChecklistItem

__all__ = [
    "Assessment",
    "AssessmentAnswer",
    "AssessmentQuestion",
    "AssessmentVersion",
    "Habit",
    "HabitLog",
    "HabitTemplate",
    "HabitVersion",
    "HabitVersionDay",
    "ImprovementPlan",
    "Mission",
    "MissionLog",
    "Profile",
    "Project",
    "Task",
    "TaskChecklistItem",
    "UserGameProfile",
    "UserSettings",
    "WeeklyReview",
    "XpRule",
    "XpTransaction",
]
