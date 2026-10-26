"""UNIQUE rules: one occurrence per date, one active plan, one open assessment, ..."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.domain.enums import LogStatus
from app.models import (
    Assessment,
    AssessmentAnswer,
    AssessmentQuestion,
    AssessmentVersion,
    Habit,
    HabitLog,
    HabitVersion,
    ImprovementPlan,
    Mission,
    MissionLog,
    Project,
    TaskChecklistItem,
    WeeklyReview,
)
from app.services import habits as habit_service
from app.services import missions as mission_service
from tests.factories import MONDAY, NOW, TODAY, make_habit, make_mission, make_plan, make_project, make_task


def rejected(session, *objects):
    for obj in objects:
        session.add(obj)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def _version_id(session, habit):
    return session.execute(select(HabitVersion.id).where(HabitVersion.habit_id == habit.id)).scalar_one()


# --- occurrences ---------------------------------------------------------------------------
def test_database_rejects_a_second_log_for_the_same_habit_and_date(session, user_id):
    habit = make_habit(session, user_id)
    vid = _version_id(session, habit)
    session.add(HabitLog(user_id=user_id, habit_id=habit.id, habit_version_id=vid, occurrence_date=TODAY, status="completed"))
    session.commit()
    rejected(session, HabitLog(user_id=user_id, habit_id=habit.id, habit_version_id=vid, occurrence_date=TODAY, status="partial"))


def test_the_same_date_is_fine_for_different_habits(session, user_id):
    a, b = make_habit(session, user_id, name="A"), make_habit(session, user_id, name="B")
    for habit in (a, b):
        session.add(
            HabitLog(user_id=user_id, habit_id=habit.id, habit_version_id=_version_id(session, habit), occurrence_date=TODAY, status="completed")
        )
    session.commit()


def test_logging_twice_through_the_service_updates_instead_of_duplicating(session, reference_data, user_id):
    habit = make_habit(session, user_id)
    first = habit_service.log_habit(session, user_id, habit.id, TODAY, now=NOW)
    second = habit_service.log_habit(session, user_id, habit.id, TODAY, now=NOW)
    session.commit()
    assert first.log.id == second.log.id
    assert session.execute(select(text("count(*)")).select_from(HabitLog)).scalar_one() == 1


def test_database_rejects_a_second_mission_log_for_the_same_date(session, reference_data, user_id):
    mission = make_mission(session, user_id, make_plan(session, user_id))
    session.add(MissionLog(user_id=user_id, mission_id=mission.id, occurrence_date=MONDAY, status="completed"))
    session.commit()
    rejected(session, MissionLog(user_id=user_id, mission_id=mission.id, occurrence_date=MONDAY, status="partial"))


def test_mission_log_service_upserts(session, reference_data, user_id):
    mission = make_mission(session, user_id, make_plan(session, user_id))
    a = mission_service.log_mission(session, user_id, mission.id, MONDAY, status=LogStatus.PARTIAL, now=NOW)
    b = mission_service.log_mission(session, user_id, mission.id, MONDAY, status=LogStatus.COMPLETED, now=NOW)
    session.commit()
    assert a.log.id == b.log.id and b.log.status == "completed"


# --- habits / versions / projects ---------------------------------------------------------
def test_two_versions_cannot_start_the_same_day(session, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1))
    rejected(
        session,
        HabitVersion(habit_id=habit.id, effective_from=date(2026, 10, 1), goal_type="binary", frequency_type="daily"),
    )


def test_active_habit_names_are_unique_per_user_ignoring_case(session, user_id, other_user_id):
    make_habit(session, user_id, name="Meditate")
    with pytest.raises(IntegrityError):
        make_habit(session, user_id, name="meditate")
    session.rollback()
    make_habit(session, other_user_id, name="Meditate")  # another user may reuse it


def test_archived_habit_frees_its_name(session, user_id):
    from datetime import datetime, UTC
    from app.domain.enums import HabitStatus

    habit = make_habit(session, user_id, name="Run")
    habit_service.set_habit_status(session, user_id, habit.id, HabitStatus.ARCHIVED, datetime.now(UTC))
    session.commit()
    make_habit(session, user_id, name="Run")


def test_open_project_names_are_unique_per_user(session, user_id):
    make_project(session, user_id, "Thesis")
    rejected(session, Project(user_id=user_id, name="THESIS"))


# --- plans / assessments -------------------------------------------------------------------
def test_one_active_plan_per_user(session, reference_data, user_id):
    first = make_plan(session, user_id)
    version_id = first and session.execute(select(AssessmentVersion.id)).scalars().first()
    second_assessment = Assessment(user_id=user_id, assessment_version_id=version_id, status="abandoned")
    session.add(second_assessment)
    session.commit()
    rejected(session, ImprovementPlan(user_id=user_id, assessment_id=second_assessment.id, started_on=MONDAY))


def test_a_completed_plan_does_not_block_a_new_active_one(session, reference_data, user_id):
    first = make_plan(session, user_id)
    first.status, first.ended_on = "completed", date(2026, 10, 25)
    session.commit()
    version_id = session.execute(select(AssessmentVersion.id)).scalars().first()
    a2 = Assessment(user_id=user_id, assessment_version_id=version_id, status="abandoned")
    session.add(a2)
    session.commit()
    session.add(ImprovementPlan(user_id=user_id, assessment_id=a2.id, started_on=date(2026, 11, 2)))
    session.commit()


def test_one_plan_per_assessment(session, reference_data, user_id):
    plan = make_plan(session, user_id)
    rejected(session, ImprovementPlan(user_id=user_id, assessment_id=plan.assessment_id, status="archived", started_on=MONDAY, ended_on=MONDAY))


def test_one_open_assessment_per_user(session, reference_data, user_id):
    version_id = session.execute(select(AssessmentVersion.id)).scalars().first()
    session.add(Assessment(user_id=user_id, assessment_version_id=version_id))
    session.commit()
    rejected(session, Assessment(user_id=user_id, assessment_version_id=version_id))


def test_only_one_questionnaire_version_can_be_active(session, reference_data):
    rejected(session, AssessmentVersion(version=2, title="v2", is_active=True))  # v1 is already active
    session.add(AssessmentVersion(version=2, title="v2", is_active=False))
    session.commit()


def test_an_assessment_has_one_answer_per_question(session, reference_data, user_id):
    plan = make_plan(session, user_id)
    q = session.execute(select(AssessmentQuestion).limit(1)).scalar_one()
    kwargs = dict(user_id=user_id, assessment_id=plan.assessment_id, assessment_version_id=q.assessment_version_id, question_id=q.id)
    session.add(AssessmentAnswer(value_number=3, **kwargs))
    session.commit()
    rejected(session, AssessmentAnswer(value_number=4, **kwargs))


def test_one_weekly_review_per_plan_and_week(session, reference_data, user_id):
    plan = make_plan(session, user_id)
    kw = dict(user_id=user_id, plan_id=plan.id, week_start=MONDAY, perceived_difficulty=3, energy_level=3)
    session.add(WeeklyReview(**kw))
    session.commit()
    rejected(session, WeeklyReview(**kw))


def test_one_mission_per_template_plan_and_week(session, reference_data, user_id):
    plan = make_plan(session, user_id)
    mission = make_mission(session, user_id, plan)
    rejected(
        session,
        Mission(user_id=user_id, plan_id=plan.id, habit_template_id=mission.habit_template_id, week_start=MONDAY, weekly_target_count=2),
    )


def test_catalog_versions_are_unique(session, reference_data):
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO habit_templates (code, version, title, category, goal_type, default_weekly_target_count, difficulty)"
                " VALUES ('drink_water', 1, 'dup', 'hydration', 'binary', 3, 1)"
            )
        )
        session.commit()


# --- checklist ordering ---------------------------------------------------------------------
def test_checklist_positions_are_unique_but_can_be_swapped_in_one_transaction(session, user_id):
    task = make_task(session, user_id)
    a = TaskChecklistItem(task_id=task.id, title="a", position=0)
    b = TaskChecklistItem(task_id=task.id, title="b", position=1)
    session.add_all([a, b])
    session.commit()
    rejected(session, TaskChecklistItem(task_id=task.id, title="dup", position=1))

    # Deferred uniqueness: a swap does not fail midway.
    session.execute(text("UPDATE task_checklist_items SET position = 1 WHERE id = :id"), {"id": a.id})
    session.execute(text("UPDATE task_checklist_items SET position = 0 WHERE id = :id"), {"id": b.id})
    session.commit()
    order = session.execute(select(TaskChecklistItem.title).order_by(TaskChecklistItem.position)).scalars().all()
    assert order == ["b", "a"]
