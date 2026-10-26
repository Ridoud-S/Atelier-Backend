"""Habit versions: no overlaps/gaps, future & retroactive changes, immutability, locking."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.domain.enums import FrequencyType, GoalType, LogStatus
from app.models import Habit, HabitLog, HabitVersion
from app.services import habits as svc
from app.services.errors import ValidationFailed
from app.services.habits import HabitSpec
from tests.factories import NOW, TODAY, make_habit, make_user

WATER_2L = HabitSpec(goal_type=GoalType.QUANTITY, target_value=Decimal("2"), unit="L")
WATER_3L = HabitSpec(goal_type=GoalType.QUANTITY, target_value=Decimal("3"), unit="L")


def versions(session, habit):
    return session.execute(
        select(HabitVersion).where(HabitVersion.habit_id == habit.id).order_by(HabitVersion.effective_from)
    ).scalars().all()


# --- creation / "no unexpected gaps" ------------------------------------------------------
def test_a_habit_cannot_be_committed_without_a_version(session, user_id):
    session.add(Habit(user_id=user_id, name="orphan", category="focus"))
    with pytest.raises(IntegrityError, match="at least one version"):
        session.commit()


def test_the_last_version_cannot_be_deleted(session, user_id):
    habit = make_habit(session, user_id)
    with pytest.raises(IntegrityError, match="at least one version"):
        session.execute(text("DELETE FROM habit_versions WHERE habit_id = :h"), {"h": habit.id})
        session.commit()


def test_start_date_is_the_first_versions_effective_from(session, user_id):
    habit = make_habit(session, user_id, start=date(2026, 9, 14))
    assert versions(session, habit)[0].effective_from == date(2026, 9, 14)


def test_no_overlap_by_construction(session, user_id):
    """A version has no end date; two cannot start on the same day => periods are disjoint."""
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    svc.add_version(session, user_id, habit.id, effective_from=date(2026, 11, 1), spec=WATER_3L)
    session.commit()
    with pytest.raises(IntegrityError):
        svc.add_version(session, user_id, habit.id, effective_from=date(2026, 11, 1), spec=WATER_2L)
    session.rollback()
    cols = {c["name"] for c in session.get_bind().dialect.get_columns(session.connection(), "habit_versions")}
    assert "effective_to" not in cols and "valid_range" not in cols


# --- which version governs a date ----------------------------------------------------------
def test_log_uses_the_version_in_force_on_its_date(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    v1 = versions(session, habit)[0]
    svc.log_habit(session, user_id, habit.id, date(2026, 10, 20), value=Decimal("2"), now=NOW)
    v2 = svc.add_version(session, user_id, habit.id, effective_from=date(2026, 10, 24), spec=WATER_3L)
    session.commit()
    before = svc.log_habit(session, user_id, habit.id, date(2026, 10, 24), value=Decimal("2.5"), now=NOW)
    session.commit()
    assert before.log.habit_version_id == v2.id  # 24th is governed by v2 (3 L) => 2.5 L is partial
    assert before.log.status == LogStatus.PARTIAL
    old = session.execute(select(HabitLog).where(HabitLog.occurrence_date == date(2026, 10, 20))).scalar_one()
    assert old.habit_version_id == v1.id and old.status == LogStatus.COMPLETED


def test_old_results_are_not_recomputed_with_the_current_goal(session, reference_data, user_id):
    """2 L counted as completed in October stays completed after the goal rises to 3 L."""
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    svc.log_habit(session, user_id, habit.id, date(2026, 10, 10), value=Decimal("2"), now=NOW)
    svc.add_version(session, user_id, habit.id, effective_from=date(2026, 10, 26), spec=WATER_3L)
    session.commit()
    log = session.execute(select(HabitLog).where(HabitLog.occurrence_date == date(2026, 10, 10))).scalar_one()
    version = session.get(HabitVersion, log.habit_version_id)
    assert (log.status, log.value, version.target_value) == (LogStatus.COMPLETED, Decimal("2.000"), Decimal("2.000"))


def test_logging_before_the_habit_started_is_rejected(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 10))
    with pytest.raises(ValidationFailed, match="no version in force"):
        svc.log_habit(session, user_id, habit.id, date(2026, 10, 9), now=NOW)


def test_database_rejects_a_log_that_uses_the_wrong_version(session, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    v1 = versions(session, habit)[0]
    svc.add_version(session, user_id, habit.id, effective_from=date(2026, 10, 15), spec=WATER_3L)
    session.commit()
    session.add(
        HabitLog(user_id=user_id, habit_id=habit.id, habit_version_id=v1.id, occurrence_date=date(2026, 10, 20), status="completed", value=2)
    )
    with pytest.raises(IntegrityError, match="not governed by habit version"):
        session.commit()


def test_database_rejects_a_log_before_any_version(session, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 10))
    vid = versions(session, habit)[0].id
    session.add(HabitLog(user_id=user_id, habit_id=habit.id, habit_version_id=vid, occurrence_date=date(2026, 10, 1), status="completed"))
    with pytest.raises(IntegrityError, match="not governed"):
        session.commit()


def test_future_dates_cannot_be_logged(session, reference_data, user_id):
    habit = make_habit(session, user_id)
    with pytest.raises(ValidationFailed, match="future"):
        svc.log_habit(session, user_id, habit.id, date(2026, 10, 26), now=NOW)


# --- future versions -----------------------------------------------------------------------
def test_a_future_version_takes_effect_on_its_date_only(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    future = svc.add_version(session, user_id, habit.id, effective_from=date(2026, 12, 1), spec=WATER_3L)
    session.commit()
    assert svc.applicable_version(session, habit.id, date(2026, 11, 30)).id != future.id
    assert svc.applicable_version(session, habit.id, date(2026, 12, 1)).id == future.id
    assert svc.applicable_version(session, habit.id, date(2027, 6, 1)).id == future.id


def test_an_unlogged_future_version_can_still_be_edited_and_deleted(session, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    future = svc.add_version(session, user_id, habit.id, effective_from=date(2026, 12, 1), spec=WATER_3L)
    session.commit()
    future.target_value = Decimal("4")
    session.commit()  # allowed: nobody has logged against it
    session.delete(future)
    session.commit()
    assert len(versions(session, habit)) == 1


# --- retroactive changes -------------------------------------------------------------------
def test_retroactive_version_is_rejected_when_it_would_reinterpret_logged_days(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    svc.log_habit(session, user_id, habit.id, date(2026, 10, 20), value=Decimal("2"), now=NOW)
    session.commit()
    for retro in (date(2026, 10, 10), date(2026, 10, 20)):  # before AND on the last logged day
        with pytest.raises(IntegrityError, match="retroactive"):
            svc.add_version(session, user_id, habit.id, effective_from=retro, spec=WATER_3L)
        session.rollback()
    assert len(versions(session, habit)) == 1


def test_a_past_version_is_allowed_after_the_last_logged_day(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    svc.log_habit(session, user_id, habit.id, date(2026, 10, 20), value=Decimal("2"), now=NOW)
    svc.add_version(session, user_id, habit.id, effective_from=date(2026, 10, 21), spec=WATER_3L)  # already past, no logs there
    session.commit()
    assert svc.applicable_version(session, habit.id, date(2026, 10, 22)).target_value == Decimal("3.000")


def test_a_retroactive_version_is_fine_for_a_habit_with_no_logs(session, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 10), spec=WATER_2L)
    svc.add_version(session, user_id, habit.id, effective_from=date(2026, 10, 12), spec=WATER_3L)
    session.commit()


def test_a_version_in_the_middle_cannot_rewrite_logged_history(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    svc.add_version(session, user_id, habit.id, effective_from=date(2026, 10, 20), spec=WATER_3L)
    svc.log_habit(session, user_id, habit.id, date(2026, 10, 22), value=Decimal("3"), now=NOW)
    session.commit()
    with pytest.raises(IntegrityError, match="retroactive"):
        svc.add_version(session, user_id, habit.id, effective_from=date(2026, 10, 15), spec=WATER_2L)


# --- immutability once referenced ------------------------------------------------------------
def test_a_version_with_logs_is_immutable(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    svc.log_habit(session, user_id, habit.id, date(2026, 10, 20), value=Decimal("2"), now=NOW)
    session.commit()
    v1 = versions(session, habit)[0]
    for column, value in [("target_value", Decimal("9")), ("unit", "ml"), ("effective_from", date(2026, 9, 1))]:
        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(text(f"UPDATE habit_versions SET {column} = :v WHERE id = :id"), {"v": value, "id": v1.id})
            session.commit()
        session.rollback()
    with pytest.raises(IntegrityError, match="immutable"):
        session.execute(text("UPDATE habit_versions SET frequency_type = 'times_per_week', weekly_target_count = 3 WHERE id = :id"), {"id": v1.id})
        session.commit()
    session.rollback()
    with pytest.raises(IntegrityError):  # and cannot be deleted
        session.execute(text("DELETE FROM habit_versions WHERE id = :id"), {"id": v1.id})
        session.commit()
    session.rollback()


def test_touching_updated_at_on_a_logged_version_is_not_a_semantic_change(session, reference_data, user_id):
    habit = make_habit(session, user_id, spec=WATER_2L)
    svc.log_habit(session, user_id, habit.id, TODAY, value=Decimal("2"), now=NOW)
    session.commit()
    session.execute(text("UPDATE habit_versions SET updated_at = now() WHERE habit_id = :h"), {"h": habit.id})
    session.commit()


def test_weekdays_of_a_logged_version_are_immutable(session, reference_data, user_id):
    spec = HabitSpec(frequency_type=FrequencyType.WEEKDAYS, weekdays=(1, 3, 5))
    habit = make_habit(session, user_id, spec=spec)
    vid = versions(session, habit)[0].id
    svc.log_habit(session, user_id, habit.id, TODAY, now=NOW)
    session.commit()
    for sql in (
        "DELETE FROM habit_version_days WHERE habit_version_id = :v AND weekday = 1",
        "INSERT INTO habit_version_days (habit_version_id, weekday) VALUES (:v, 2)",
        "UPDATE habit_version_days SET weekday = 7 WHERE habit_version_id = :v AND weekday = 1",
    ):
        with pytest.raises(IntegrityError, match="immutable"):
            session.execute(text(sql), {"v": vid})
            session.commit()
        session.rollback()


# --- end date ----------------------------------------------------------------------------------
def test_no_logs_or_versions_after_the_habit_end_date(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), end_date=date(2026, 10, 15))
    svc.log_habit(session, user_id, habit.id, date(2026, 10, 15), now=NOW)
    session.commit()
    with pytest.raises(IntegrityError, match="end_date"):
        svc.log_habit(session, user_id, habit.id, date(2026, 10, 16), now=NOW)
    session.rollback()
    with pytest.raises(IntegrityError, match="end_date"):
        svc.add_version(session, user_id, habit.id, effective_from=date(2026, 10, 20), spec=WATER_3L)


# --- backend validation mirrors the CHECKs ---------------------------------------------------
@pytest.mark.parametrize(
    "spec",
    [
        HabitSpec(goal_type=GoalType.QUANTITY, target_value=Decimal("2")),  # no unit
        HabitSpec(goal_type=GoalType.DURATION, target_value=Decimal("30"), unit="hours"),
        HabitSpec(goal_type=GoalType.BINARY, target_value=Decimal("1")),
        HabitSpec(frequency_type=FrequencyType.TIMES_PER_WEEK),
        HabitSpec(frequency_type=FrequencyType.TIMES_PER_WEEK, weekly_target_count=9),
        HabitSpec(frequency_type=FrequencyType.WEEKDAYS),  # no days
        HabitSpec(frequency_type=FrequencyType.WEEKDAYS, weekdays=(1, 1)),
        HabitSpec(frequency_type=FrequencyType.WEEKDAYS, weekdays=(0,)),
        HabitSpec(frequency_type=FrequencyType.DAILY, weekdays=(1,)),
    ],
)
def test_backend_validation_rejects_inconsistent_specs(session, user_id, spec):
    with pytest.raises(ValidationFailed):
        svc.validate_spec(spec)


# --- locking between version changes and logging ----------------------------------------------
def test_creating_a_version_blocks_concurrent_logging_of_that_habit(session, session_factory, reference_data, user_id):
    """The habit row is the serialization point: a pending version change makes a concurrent
    log wait instead of racing it (it would otherwise slip in under the old version)."""
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    session.commit()

    svc.add_version(session, user_id, habit.id, effective_from=date(2026, 10, 24), spec=WATER_3L)  # uncommitted

    with session_factory() as other:
        other.execute(text("SET LOCAL lock_timeout = '300ms'"))
        with pytest.raises(DBAPIError, match="lock timeout|canceling statement"):
            svc.log_habit(other, user_id, habit.id, date(2026, 10, 25), value=Decimal("3"), now=NOW)
        other.rollback()
    session.rollback()


def test_a_log_cannot_end_up_under_a_stale_version_when_racing_a_version_change(
    session, engine, session_factory, reference_data, user_id
):
    """Widen the race window on purpose: a temporary slow trigger runs *after* the log guard
    has read the versions. A version change for the same date starts 0.3 s later. Whatever
    the interleaving, history must stay consistent (the loser is rejected)."""
    import threading
    import time

    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=WATER_2L)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE FUNCTION test_slow_log() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN PERFORM pg_sleep(0.8); RETURN NEW; END $$"
        ))
        conn.execute(text(
            "CREATE TRIGGER trg_zz_slow BEFORE INSERT ON habit_logs "
            "FOR EACH ROW EXECUTE FUNCTION test_slow_log()"
        ))  # fires after trg_habit_logs_guard (alphabetical order)
    outcome: dict[str, str] = {}

    def log_it():
        with session_factory() as s:
            try:
                svc.log_habit(s, user_id, habit.id, date(2026, 10, 24), value=Decimal("2"), now=NOW)
                s.commit()
                outcome["log"] = "ok"
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                outcome["log"] = type(exc).__name__

    def add_version():
        time.sleep(0.3)
        with session_factory() as s:
            try:
                svc.add_version(s, user_id, habit.id, effective_from=date(2026, 10, 24), spec=WATER_3L)
                s.commit()
                outcome["version"] = "ok"
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                outcome["version"] = type(exc).__name__

    try:
        threads = [threading.Thread(target=log_it), threading.Thread(target=add_version)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS trg_zz_slow ON habit_logs"))
            conn.execute(text("DROP FUNCTION IF EXISTS test_slow_log()"))

    stale = session.execute(text(
        """
        SELECT count(*) FROM habit_logs l
        WHERE l.habit_version_id <> (
            SELECT v.id FROM habit_versions v
            WHERE v.habit_id = l.habit_id AND v.effective_from <= l.occurrence_date
            ORDER BY v.effective_from DESC LIMIT 1)
        """
    )).scalar_one()
    assert stale == 0, f"a log was left under a stale version: {outcome}"
    assert not (outcome["log"] == "ok" and outcome["version"] == "ok"), outcome
