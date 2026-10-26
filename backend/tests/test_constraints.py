"""Valid values (CHECK constraints) and allowed state transitions."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.domain import transitions as tr
from app.domain.enums import (
    AssessmentStatus,
    HabitStatus,
    MissionStatus,
    PlanStatus,
    ProjectStatus,
    TaskStatus,
)
from app.services.errors import InvalidTransitionError
from tests.factories import MONDAY, TODAY, make_habit, make_mission, make_plan, make_project, make_task


def sql_rejected(session, sql, params=None):
    with pytest.raises(IntegrityError):
        session.execute(text(sql), params or {})
        session.commit()
    session.rollback()


# --- habits ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "column, value",
    [
        ("category", "gym"),
        ("status", "deleted"),
        ("origin", "robot"),
    ],
)
def test_habit_enumerations(session, user_id, column, value):
    base = {"category": "focus", "status": "active", "origin": "user"}
    base[column] = value
    sql_rejected(
        session,
        f"INSERT INTO habits (user_id, name, category, status, origin) VALUES (:u, 'h', '{base['category']}', '{base['status']}', '{base['origin']}')",
        {"u": user_id},
    )


def test_habit_archived_flag_and_timestamp_must_agree(session, user_id):
    habit = make_habit(session, user_id)
    sql_rejected(session, "UPDATE habits SET status = 'archived' WHERE id = :id", {"id": habit.id})
    sql_rejected(session, "UPDATE habits SET archived_at = now() WHERE id = :id", {"id": habit.id})


def test_user_habit_cannot_carry_a_template(session, reference_data, user_id):
    template_id = session.execute(text("SELECT id FROM habit_templates LIMIT 1")).scalar_one()
    sql_rejected(
        session,
        "INSERT INTO habits (user_id, name, category, origin, source_template_id) VALUES (:u, 'h', 'focus', 'user', :t)",
        {"u": user_id, "t": template_id},
    )


@pytest.mark.parametrize(
    "goal_type, target, unit",
    [
        ("binary", 1, None),  # binary has no target
        ("binary", None, "L"),  # ...nor unit
        ("quantity", None, "L"),  # quantity needs a target
        ("quantity", 2, None),  # ...and a unit
        ("quantity", 0, "L"),  # target must be positive
        ("duration", 30, "hours"),  # duration is in minutes
        ("distance", 5, "km"),  # unknown goal type
    ],
)
def test_goal_shape_is_enforced(session, user_id, goal_type, target, unit):
    habit = make_habit(session, user_id)
    sql_rejected(
        session,
        "INSERT INTO habit_versions (habit_id, effective_from, goal_type, target_value, unit, frequency_type)"
        " VALUES (:h, '2026-11-01', :g, :t, :u, 'daily')",
        {"h": habit.id, "g": goal_type, "t": target, "u": unit},
    )


@pytest.mark.parametrize(
    "frequency, weekly",
    [("times_per_week", None), ("times_per_week", 0), ("times_per_week", 8), ("daily", 3), ("weekdays", 2), ("monthly", None)],
)
def test_frequency_shape_is_enforced(session, user_id, frequency, weekly):
    habit = make_habit(session, user_id)
    sql_rejected(
        session,
        "INSERT INTO habit_versions (habit_id, effective_from, goal_type, frequency_type, weekly_target_count)"
        " VALUES (:h, '2026-11-01', 'binary', :f, :w)",
        {"h": habit.id, "f": frequency, "w": weekly},
    )


@pytest.mark.parametrize("weekday", [0, 8, -1])
def test_weekday_range(session, user_id, weekday):
    from app.domain.enums import FrequencyType
    from app.services.habits import HabitSpec

    habit = make_habit(session, user_id, spec=HabitSpec(frequency_type=FrequencyType.WEEKDAYS, weekdays=(1,)))
    vid = session.execute(text("SELECT id FROM habit_versions WHERE habit_id = :h"), {"h": habit.id}).scalar_one()
    sql_rejected(session, "INSERT INTO habit_version_days (habit_version_id, weekday) VALUES (:v, :d)", {"v": vid, "d": weekday})


@pytest.mark.parametrize(
    "status, value",
    [("done", None), ("completed", -1), ("skipped", 5), ("partial", -0.5)],
)
def test_habit_log_status_and_value_rules(session, user_id, status, value):
    habit = make_habit(session, user_id)
    vid = session.execute(text("SELECT id FROM habit_versions WHERE habit_id = :h"), {"h": habit.id}).scalar_one()
    sql_rejected(
        session,
        "INSERT INTO habit_logs (user_id, habit_id, habit_version_id, occurrence_date, status, value)"
        " VALUES (:u, :h, :v, :d, :s, :val)",
        {"u": user_id, "h": habit.id, "v": vid, "d": TODAY, "s": status, "val": value},
    )


# --- tasks / projects -------------------------------------------------------------------------
@pytest.mark.parametrize("column, value", [("status", "blocked"), ("priority", "urgent")])
def test_task_enumerations(session, user_id, column, value):
    task = make_task(session, user_id)
    sql_rejected(session, f"UPDATE tasks SET {column} = :v WHERE id = :id", {"v": value, "id": task.id})


def test_task_done_and_completed_at_must_agree(session, user_id):
    task = make_task(session, user_id)
    sql_rejected(session, "UPDATE tasks SET status = 'done' WHERE id = :id", {"id": task.id})
    sql_rejected(session, "UPDATE tasks SET completed_at = now() WHERE id = :id", {"id": task.id})


def test_task_due_date_cannot_precede_scheduled_date(session, user_id):
    sql_rejected(
        session,
        "INSERT INTO tasks (user_id, title, scheduled_date, due_date) VALUES (:u, 't', '2026-10-20', '2026-10-19')",
        {"u": user_id},
    )


def test_project_status_and_archive_timestamp(session, user_id):
    project = make_project(session, user_id)
    sql_rejected(session, "UPDATE projects SET status = 'paused' WHERE id = :id", {"id": project.id})
    sql_rejected(session, "UPDATE projects SET status = 'archived' WHERE id = :id", {"id": project.id})


def test_checklist_position_cannot_be_negative(session, user_id):
    task = make_task(session, user_id)
    sql_rejected(session, "INSERT INTO task_checklist_items (task_id, title, position) VALUES (:t, 'x', -1)", {"t": task.id})


# --- assessment / plans / missions ------------------------------------------------------------
def test_assessment_status_and_completion_timestamp(session, reference_data, user_id):
    plan = make_plan(session, user_id)
    sql_rejected(session, "UPDATE assessments SET status = 'paused' WHERE id = :id", {"id": plan.assessment_id})
    sql_rejected(session, "UPDATE assessments SET status = 'completed' WHERE id = :id", {"id": plan.assessment_id})


def test_answer_needs_exactly_one_value(session, reference_data, user_id):
    plan = make_plan(session, user_id)
    q = session.execute(text("SELECT id, assessment_version_id FROM assessment_questions LIMIT 1")).one()
    base = {"u": user_id, "a": plan.assessment_id, "v": q.assessment_version_id, "q": q.id}
    sql_rejected(
        session,
        "INSERT INTO assessment_answers (user_id, assessment_id, assessment_version_id, question_id) VALUES (:u, :a, :v, :q)",
        base,
    )
    sql_rejected(
        session,
        "INSERT INTO assessment_answers (user_id, assessment_id, assessment_version_id, question_id, value_number, value_text)"
        " VALUES (:u, :a, :v, :q, 3, 'three')",
        base,
    )


def test_plan_end_rules(session, reference_data, user_id):
    plan = make_plan(session, user_id)
    sql_rejected(session, "UPDATE improvement_plans SET status = 'completed' WHERE id = :id", {"id": plan.id})  # no ended_on
    sql_rejected(session, "UPDATE improvement_plans SET ended_on = '2026-10-20' WHERE id = :id", {"id": plan.id})  # still active
    sql_rejected(
        session,
        "UPDATE improvement_plans SET status = 'completed', ended_on = '2026-10-01' WHERE id = :id",
        {"id": plan.id},
    )  # ends before it started


@pytest.mark.parametrize("target", [0, 8])
def test_mission_weekly_target_range(session, reference_data, user_id, target):
    plan = make_plan(session, user_id)
    template_id = session.execute(text("SELECT id FROM habit_templates LIMIT 1")).scalar_one()
    sql_rejected(
        session,
        "INSERT INTO missions (user_id, plan_id, habit_template_id, week_start, weekly_target_count)"
        " VALUES (:u, :p, :t, :w, :n)",
        {"u": user_id, "p": plan.id, "t": template_id, "w": MONDAY, "n": target},
    )


def test_mission_log_difficulty_range(session, reference_data, user_id):
    mission = make_mission(session, user_id, make_plan(session, user_id))
    for bad in (0, 6):
        sql_rejected(
            session,
            "INSERT INTO mission_logs (user_id, mission_id, occurrence_date, status, perceived_difficulty)"
            " VALUES (:u, :m, :d, 'completed', :x)",
            {"u": user_id, "m": mission.id, "d": MONDAY, "x": bad},
        )


@pytest.mark.parametrize("difficulty, energy", [(0, 3), (6, 3), (3, 0), (3, 6)])
def test_weekly_review_scales(session, reference_data, user_id, difficulty, energy):
    plan = make_plan(session, user_id)
    sql_rejected(
        session,
        "INSERT INTO weekly_reviews (user_id, plan_id, week_start, perceived_difficulty, energy_level)"
        " VALUES (:u, :p, :w, :d, :e)",
        {"u": user_id, "p": plan.id, "w": MONDAY, "d": difficulty, "e": energy},
    )


def test_xp_rule_values(session):
    sql_rejected(session, "INSERT INTO xp_rules (rule_code, effective_from, source_type, xp_amount) VALUES ('x', '2026-01-01', 'task', -1)")
    sql_rejected(session, "INSERT INTO xp_rules (rule_code, effective_from, source_type, xp_amount) VALUES ('x', '2026-01-01', 'quest', 1)")
    sql_rejected(session, "INSERT INTO xp_rules (rule_code, effective_from, source_type, xp_amount, daily_cap) VALUES ('x', '2026-01-01', 'task', 1, -5)")


# --- transitions -----------------------------------------------------------------------------
VALID = [
    (tr.HABIT_TRANSITIONS, HabitStatus.ACTIVE, HabitStatus.ARCHIVED),
    (tr.HABIT_TRANSITIONS, HabitStatus.ARCHIVED, HabitStatus.ACTIVE),
    (tr.PROJECT_TRANSITIONS, ProjectStatus.ACTIVE, ProjectStatus.COMPLETED),
    (tr.PROJECT_TRANSITIONS, ProjectStatus.COMPLETED, ProjectStatus.ARCHIVED),
    (tr.PROJECT_TRANSITIONS, ProjectStatus.ARCHIVED, ProjectStatus.ACTIVE),
    (tr.TASK_TRANSITIONS, TaskStatus.TODO, TaskStatus.DONE),
    (tr.TASK_TRANSITIONS, TaskStatus.DONE, TaskStatus.TODO),  # reopen
    (tr.TASK_TRANSITIONS, TaskStatus.CANCELLED, TaskStatus.TODO),
    (tr.ASSESSMENT_TRANSITIONS, AssessmentStatus.IN_PROGRESS, AssessmentStatus.COMPLETED),
    (tr.PLAN_TRANSITIONS, PlanStatus.ACTIVE, PlanStatus.ARCHIVED),
    (tr.MISSION_TRANSITIONS, MissionStatus.ACTIVE, MissionStatus.SUPERSEDED),
]
INVALID = [
    (tr.PROJECT_TRANSITIONS, ProjectStatus.COMPLETED, ProjectStatus.COMPLETED),
    (tr.TASK_TRANSITIONS, TaskStatus.CANCELLED, TaskStatus.DONE),
    (tr.TASK_TRANSITIONS, TaskStatus.DONE, TaskStatus.CANCELLED),
    (tr.ASSESSMENT_TRANSITIONS, AssessmentStatus.COMPLETED, AssessmentStatus.IN_PROGRESS),
    (tr.ASSESSMENT_TRANSITIONS, AssessmentStatus.ABANDONED, AssessmentStatus.COMPLETED),
    (tr.PLAN_TRANSITIONS, PlanStatus.ARCHIVED, PlanStatus.ACTIVE),
    (tr.PLAN_TRANSITIONS, PlanStatus.COMPLETED, PlanStatus.ACTIVE),
    (tr.MISSION_TRANSITIONS, MissionStatus.COMPLETED, MissionStatus.ACTIVE),
    (tr.MISSION_TRANSITIONS, MissionStatus.SUPERSEDED, MissionStatus.COMPLETED),
]


@pytest.mark.parametrize("table, current, target", VALID)
def test_valid_transitions(table, current, target):
    tr.assert_transition(table, current, target)


@pytest.mark.parametrize("table, current, target", INVALID)
def test_invalid_transitions(table, current, target):
    with pytest.raises(InvalidTransitionError):
        tr.assert_transition(table, current, target)


def test_every_state_of_every_machine_is_declared():
    for table, enum in [
        (tr.HABIT_TRANSITIONS, HabitStatus),
        (tr.PROJECT_TRANSITIONS, ProjectStatus),
        (tr.TASK_TRANSITIONS, TaskStatus),
        (tr.ASSESSMENT_TRANSITIONS, AssessmentStatus),
        (tr.PLAN_TRANSITIONS, PlanStatus),
        (tr.MISSION_TRANSITIONS, MissionStatus),
    ]:
        assert set(table) == {m.value for m in enum}
        assert all(targets <= {m.value for m in enum} for targets in table.values())


def test_service_rejects_an_invalid_task_transition(session, user_id):
    from app.services.tasks import change_task_status

    task = make_task(session, user_id)
    change_task_status(session, user_id, task.id, TaskStatus.CANCELLED)
    with pytest.raises(InvalidTransitionError):
        change_task_status(session, user_id, task.id, TaskStatus.DONE)
