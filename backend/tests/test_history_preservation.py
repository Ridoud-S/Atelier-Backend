"""Archiving keeps history; only history-free rows can be physically deleted."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.domain.enums import HabitStatus, LogStatus, MissionStatus, PlanStatus, ProjectStatus, TaskStatus
from app.models import (
    AssessmentAnswer,
    AssessmentQuestion,
    AssessmentVersion,
    Habit,
    HabitLog,
    HabitTemplate,
    HabitVersion,
    ImprovementPlan,
    Mission,
    MissionLog,
    Project,
    Task,
    UserSettings,
    XpTransaction,
)
from app.services import habits as habit_svc
from app.services import missions as mission_svc
from app.services.errors import ConflictError
from app.services.progress import weekly_progress
from app.services.tasks import change_task_status
from app.services.xp import total_xp_from_history
from tests.factories import MONDAY, NOW, TODAY, make_habit, make_mission, make_plan, make_project, make_task


def count(session, model):
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# --- habits --------------------------------------------------------------------------------------
def test_archiving_a_habit_keeps_logs_versions_and_xp(session, reference_data, user_id):
    habit = make_habit(session, user_id)
    for n in range(3):
        habit_svc.log_habit(session, user_id, habit.id, TODAY - timedelta(days=n), now=NOW)
    session.commit()
    xp_before = total_xp_from_history(session, user_id)

    habit_svc.set_habit_status(session, user_id, habit.id, HabitStatus.ARCHIVED, datetime.now(UTC))
    session.commit()

    assert session.get(Habit, habit.id).status == "archived"
    assert session.get(Habit, habit.id).archived_at is not None
    assert count(session, HabitLog) == 3 and count(session, HabitVersion) == 1
    assert total_xp_from_history(session, user_id) == xp_before == 30
    # history stays queryable and statistics still work on archived habits
    assert weekly_progress(session, user_id, habit.id, MONDAY).completed >= 1


def test_an_archived_habit_rejects_new_logs_and_versions_but_can_be_restored(session, reference_data, user_id):
    habit = make_habit(session, user_id)
    habit_svc.set_habit_status(session, user_id, habit.id, HabitStatus.ARCHIVED, datetime.now(UTC))
    session.commit()
    with pytest.raises(ConflictError):
        habit_svc.log_habit(session, user_id, habit.id, TODAY, now=NOW)
    with pytest.raises(ConflictError):
        habit_svc.add_version(session, user_id, habit.id, effective_from=date(2026, 12, 1), spec=habit_svc.HabitSpec())

    habit_svc.set_habit_status(session, user_id, habit.id, HabitStatus.ACTIVE, datetime.now(UTC))
    session.commit()
    assert session.get(Habit, habit.id).archived_at is None
    habit_svc.log_habit(session, user_id, habit.id, TODAY, now=NOW)


def test_archiving_twice_is_a_noop(session, user_id):
    habit = make_habit(session, user_id)
    for _ in range(2):
        habit_svc.set_habit_status(session, user_id, habit.id, HabitStatus.ARCHIVED, datetime.now(UTC))
    session.commit()
    assert session.get(Habit, habit.id).status == "archived"


def test_a_habit_without_history_can_be_physically_removed(session, user_id):
    habit = make_habit(session, user_id)
    session.execute(text("DELETE FROM habit_versions WHERE habit_id = :h"), {"h": habit.id})
    session.execute(text("DELETE FROM habits WHERE id = :h"), {"h": habit.id})
    session.commit()  # the deferred "keep one version" check ignores habits that no longer exist
    assert count(session, Habit) == 0


# --- tasks and projects ---------------------------------------------------------------------------
def test_archiving_a_project_keeps_its_tasks_and_their_xp(session, reference_data, user_id):
    project = make_project(session, user_id)
    task = make_task(session, user_id, project_id=project.id)
    change_task_status(session, user_id, task.id, TaskStatus.DONE, now=NOW)
    session.commit()

    session.execute(update(Project).where(Project.id == project.id).values(status="archived", archived_at=datetime.now(UTC)))
    session.commit()

    assert session.get(Task, task.id).project_id == project.id and session.get(Task, task.id).status == "done"
    assert count(session, XpTransaction) == 1


def test_an_archived_projects_name_can_be_reused(session, user_id):
    project = make_project(session, user_id, "Thesis")
    session.execute(update(Project).where(Project.id == project.id).values(status="archived", archived_at=datetime.now(UTC)))
    session.commit()
    make_project(session, user_id, "Thesis")


def test_a_task_that_never_paid_xp_can_be_deleted_but_a_paid_one_cannot(session, reference_data, user_id):
    free, paid = make_task(session, user_id), make_task(session, user_id)
    change_task_status(session, user_id, paid.id, TaskStatus.DONE, now=NOW)
    session.commit()
    session.delete(free)
    session.commit()
    session.delete(paid)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    assert count(session, Task) == 1


# --- plans, missions, assessments -----------------------------------------------------------------------
def test_archiving_a_plan_keeps_missions_logs_and_xp(session, reference_data, user_id):
    plan = make_plan(session, user_id)
    mission = make_mission(session, user_id, plan)
    mission_svc.log_mission(session, user_id, mission.id, MONDAY, status=LogStatus.COMPLETED, now=NOW)
    session.commit()

    session.execute(update(ImprovementPlan).where(ImprovementPlan.id == plan.id).values(status="archived", ended_on=TODAY))
    session.execute(update(Mission).where(Mission.id == mission.id).values(status="abandoned"))
    session.commit()

    assert count(session, Mission) == 1 and count(session, MissionLog) == 1 and count(session, XpTransaction) == 1
    with pytest.raises(ConflictError):
        mission_svc.log_mission(session, user_id, mission.id, MONDAY + timedelta(days=1), status=LogStatus.COMPLETED, now=NOW)


def test_weekly_adaptation_creates_a_new_mission_and_keeps_the_old_one(session, reference_data, user_id):
    plan = make_plan(session, user_id)
    week1 = make_mission(session, user_id, plan, week_start=MONDAY, weekly_target_count=5)
    week2 = Mission(
        user_id=user_id, plan_id=plan.id, habit_template_id=week1.habit_template_id,
        week_start=MONDAY + timedelta(days=7), weekly_target_count=4, target_value=Decimal("6"),
        unit="glasses", adapted_from_mission_id=week1.id,
    )  # fmt: skip
    session.add(week2)
    session.execute(update(Mission).where(Mission.id == week1.id).values(status="superseded"))
    session.commit()
    old = session.get(Mission, week1.id)
    assert (old.weekly_target_count, old.status) == (5, "superseded")
    assert session.get(Mission, week2.id).adapted_from_mission_id == week1.id


def test_publishing_a_new_questionnaire_version_leaves_past_assessments_untouched(session, reference_data, user_id):
    plan = make_plan(session, user_id)
    question = session.execute(select(AssessmentQuestion).where(AssessmentQuestion.question_key == "sleep_hours")).scalar_one()
    session.add(
        AssessmentAnswer(user_id=user_id, assessment_id=plan.assessment_id, assessment_version_id=question.assessment_version_id,
                         question_id=question.id, value_number=Decimal("6.5"))  # fmt: skip
    )
    session.commit()

    # A new questionnaire is published and becomes the active one; sleep_hours is reworded.
    session.execute(update(AssessmentVersion).where(AssessmentVersion.version == 1).values(is_active=False))
    v2 = AssessmentVersion(version=2, title="v2", is_active=True)
    session.add(v2)
    session.flush()
    session.add(AssessmentQuestion(assessment_version_id=v2.id, question_key="sleep_hours", domain="sleep",
                                   prompt="New wording", answer_type="number", position=0))  # fmt: skip
    session.commit()

    answer = session.execute(select(AssessmentAnswer)).scalar_one()
    original = session.get(AssessmentQuestion, answer.question_id)
    assert original.prompt.startswith("¿Cuántas horas duermes") and answer.value_number == Decimal("6.500")
    assert session.get(AssessmentVersion, original.assessment_version_id).version == 1


def test_a_new_template_version_does_not_change_missions_already_assigned(session, reference_data, user_id):
    mission = make_mission(session, user_id, make_plan(session, user_id), template_code="drink_water")
    old_template = session.get(HabitTemplate, mission.habit_template_id)
    session.add(HabitTemplate(code="drink_water", version=2, title="Tomar agua (nueva)", category="hydration",
                              goal_type="quantity", target_value=Decimal("10"), unit="glasses",
                              default_weekly_target_count=7, difficulty=2))  # fmt: skip
    session.execute(update(HabitTemplate).where(HabitTemplate.id == old_template.id).values(is_active=False))
    session.commit()
    again = session.get(Mission, mission.id)
    assert again.habit_template_id == old_template.id
    assert session.get(HabitTemplate, again.habit_template_id).target_value == Decimal("8.000")


# --- dates ---------------------------------------------------------------------------------------------------
def test_changing_the_timezone_does_not_rewrite_stored_occurrence_dates(session, reference_data, user_id):
    habit = make_habit(session, user_id)
    habit_svc.log_habit(session, user_id, habit.id, TODAY, now=NOW)
    session.commit()
    session.execute(update(UserSettings).where(UserSettings.user_id == user_id).values(timezone="Pacific/Auckland"))
    session.commit()
    assert session.execute(select(HabitLog.occurrence_date)).scalar_one() == TODAY


# --- mission log dates --------------------------------------------------------------------------------------------
def test_a_mission_log_must_fall_inside_the_missions_week(session, reference_data, user_id):
    mission = make_mission(session, user_id, make_plan(session, user_id))
    for bad in (MONDAY - timedelta(days=1), MONDAY + timedelta(days=7)):
        session.add(MissionLog(user_id=user_id, mission_id=mission.id, occurrence_date=bad, status="completed"))
        with pytest.raises(IntegrityError, match="outside the mission week"):
            session.commit()
        session.rollback()
    session.add(MissionLog(user_id=user_id, mission_id=mission.id, occurrence_date=MONDAY + timedelta(days=6), status="completed"))
    session.commit()  # last day of the week is fine


def test_the_service_rejects_dates_outside_the_week_and_in_the_future(session, reference_data, user_id):
    from app.services.errors import ValidationFailed

    mission = make_mission(session, user_id, make_plan(session, user_id), week_start=date(2026, 10, 26))
    with pytest.raises(ValidationFailed, match="outside"):
        mission_svc.log_mission(session, user_id, mission.id, MONDAY, status=LogStatus.COMPLETED, now=NOW)
    with pytest.raises(ValidationFailed, match="future"):
        mission_svc.log_mission(session, user_id, mission.id, date(2026, 10, 27), status=LogStatus.COMPLETED, now=NOW)
