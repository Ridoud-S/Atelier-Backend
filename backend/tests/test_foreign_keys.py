"""Foreign keys, composite same-owner keys and delete policies."""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    AssessmentAnswer,
    AssessmentQuestion,
    AssessmentVersion,
    Habit,
    HabitLog,
    HabitVersion,
    ImprovementPlan,
    Mission,
    MissionLog,
    Profile,
    Project,
    Task,
    TaskChecklistItem,
    XpRule,
    XpTransaction,
)
from app.domain.enums import LogStatus
from app.services import habits as habit_service
from app.services.xp import award_xp
from app.domain.enums import XpSourceType
from tests.factories import MONDAY, NOW, TODAY, make_habit, make_mission, make_plan, make_project, make_task


def rejected(session, *objects):
    for obj in objects:
        session.add(obj)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# --- plain foreign keys ---------------------------------------------------------------------
def test_habit_for_unknown_user_is_rejected(session):
    rejected(session, Habit(user_id=uuid.uuid4(), name="x", category="focus"))


def test_task_for_unknown_user_is_rejected(session):
    rejected(session, Task(user_id=uuid.uuid4(), title="x"))


def test_task_for_unknown_project_is_rejected(session, user_id):
    rejected(session, Task(user_id=user_id, title="x", project_id=uuid.uuid4()))


def test_standalone_task_without_project_is_allowed(session, user_id):
    make_task(session, user_id)  # project_id NULL => composite FK is not evaluated


def test_habit_version_for_unknown_habit_is_rejected(session):
    rejected(session, HabitVersion(habit_id=uuid.uuid4(), effective_from=date(2026, 10, 1), goal_type="binary", frequency_type="daily"))


# --- composite same-owner keys (cross-user references die in the database) -------------------
def test_task_cannot_point_to_another_users_project(session, user_id, other_user_id):
    foreign_project = make_project(session, other_user_id)
    rejected(session, Task(user_id=user_id, title="sneaky", project_id=foreign_project.id))


def test_habit_log_cannot_point_to_another_users_habit(session, user_id, other_user_id):
    habit = make_habit(session, other_user_id)
    version_id = session.execute(select(HabitVersion.id).where(HabitVersion.habit_id == habit.id)).scalar_one()
    rejected(
        session,
        HabitLog(user_id=user_id, habit_id=habit.id, habit_version_id=version_id, occurrence_date=TODAY, status="completed"),
    )


def test_habit_log_version_must_belong_to_the_same_habit(session, user_id):
    h1 = make_habit(session, user_id, name="One")
    h2 = make_habit(session, user_id, name="Two")
    v2 = session.execute(select(HabitVersion.id).where(HabitVersion.habit_id == h2.id)).scalar_one()
    rejected(
        session,
        HabitLog(user_id=user_id, habit_id=h1.id, habit_version_id=v2, occurrence_date=TODAY, status="completed"),
    )


def test_plan_cannot_use_another_users_assessment(session, reference_data, user_id, other_user_id):
    foreign = make_plan(session, other_user_id)
    rejected(
        session,
        ImprovementPlan(user_id=user_id, assessment_id=foreign.assessment_id, started_on=MONDAY),
    )


def test_mission_cannot_join_another_users_plan(session, reference_data, user_id, other_user_id):
    foreign_plan = make_plan(session, other_user_id)
    template_id = session.execute(text("SELECT id FROM habit_templates LIMIT 1")).scalar_one()
    rejected(
        session,
        Mission(user_id=user_id, plan_id=foreign_plan.id, habit_template_id=template_id, week_start=MONDAY, weekly_target_count=3),
    )


def test_mission_log_cannot_point_to_another_users_mission(session, reference_data, user_id, other_user_id):
    mission = make_mission(session, other_user_id, make_plan(session, other_user_id))
    rejected(
        session,
        MissionLog(user_id=user_id, mission_id=mission.id, occurrence_date=MONDAY, status="completed"),
    )


def test_answer_question_must_belong_to_the_assessments_version(session, reference_data, user_id):
    plan = make_plan(session, user_id)
    v1 = session.execute(select(AssessmentVersion).where(AssessmentVersion.version == 1)).scalar_one()
    v2 = AssessmentVersion(version=2, title="v2")
    session.add(v2)
    session.flush()
    q_v2 = AssessmentQuestion(assessment_version_id=v2.id, question_key="q", domain="sleep", prompt="?", answer_type="number", position=0)
    session.add(q_v2)
    session.commit()
    # Answer belongs to an assessment of v1 but references a question of v2.
    rejected(
        session,
        AssessmentAnswer(user_id=user_id, assessment_id=plan.assessment_id, assessment_version_id=v1.id, question_id=q_v2.id, value_number=3),
    )


def test_answer_cannot_claim_another_users_assessment(session, reference_data, user_id, other_user_id):
    foreign = make_plan(session, other_user_id)
    question = session.execute(select(AssessmentQuestion).limit(1)).scalar_one()
    rejected(
        session,
        AssessmentAnswer(
            user_id=user_id,
            assessment_id=foreign.assessment_id,
            assessment_version_id=question.assessment_version_id,
            question_id=question.id,
            value_number=3,
        ),
    )


def test_xp_transaction_cannot_reference_another_users_task(session, reference_data, user_id, other_user_id):
    foreign_task = make_task(session, other_user_id)
    rule = session.execute(select(XpRule).where(XpRule.rule_code == "task.completed.medium")).scalar_one()
    rejected(
        session,
        XpTransaction(
            user_id=user_id, xp_rule_id=rule.id, task_id=foreign_task.id, amount=6, award_date=TODAY, idempotency_key="k"
        ),
    )


def test_xp_transaction_needs_exactly_one_source(session, reference_data, user_id):
    rule = session.execute(select(XpRule).limit(1)).scalar_one()
    rejected(session, XpTransaction(user_id=user_id, xp_rule_id=rule.id, amount=1, award_date=TODAY, idempotency_key="none"))
    task_a, task_b = make_task(session, user_id), make_task(session, user_id)
    rejected(
        session,
        XpTransaction(
            user_id=user_id, xp_rule_id=rule.id, task_id=task_a.id, habit_log_id=task_b.id, amount=1,
            award_date=TODAY, idempotency_key="two",
        ),
    )


# --- delete policies -------------------------------------------------------------------------
def test_user_with_data_cannot_be_deleted(session, user_id):
    make_habit(session, user_id)
    with pytest.raises(IntegrityError):
        session.execute(delete(Profile).where(Profile.id == user_id))
        session.commit()


def test_project_with_tasks_cannot_be_deleted(session, user_id):
    project = make_project(session, user_id)
    make_task(session, user_id, project_id=project.id)
    with pytest.raises(IntegrityError):
        session.execute(delete(Project).where(Project.id == project.id))
        session.commit()


def test_habit_with_logs_cannot_be_deleted(session, reference_data, user_id):
    habit = make_habit(session, user_id)
    habit_service.log_habit(session, user_id, habit.id, TODAY, now=NOW)
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(delete(Habit).where(Habit.id == habit.id))
        session.commit()


def test_checklist_items_are_removed_with_their_task(session, user_id):
    task = make_task(session, user_id)
    session.add_all([TaskChecklistItem(task_id=task.id, title="a", position=0), TaskChecklistItem(task_id=task.id, title="b", position=1)])
    session.commit()
    session.execute(delete(Task).where(Task.id == task.id))
    session.commit()
    assert session.execute(select(TaskChecklistItem)).first() is None


def test_template_referenced_by_a_mission_cannot_be_deleted(session, reference_data, user_id):
    mission = make_mission(session, user_id, make_plan(session, user_id))
    with pytest.raises(IntegrityError):
        session.execute(text("DELETE FROM habit_templates WHERE id = :id"), {"id": mission.habit_template_id})
        session.commit()
    session.rollback()


def test_xp_log_with_reward_cannot_be_deleted(session, reference_data, user_id):
    habit = make_habit(session, user_id)
    result = habit_service.log_habit(session, user_id, habit.id, TODAY, now=NOW)
    session.commit()
    assert result.xp.created
    with pytest.raises(IntegrityError):
        session.execute(delete(HabitLog).where(HabitLog.id == result.log.id))
        session.commit()
