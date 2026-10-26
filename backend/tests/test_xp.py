"""XP: idempotency, once-per-lifecycle, daily caps, immutability, rebuildability, concurrency."""
from __future__ import annotations

from datetime import date, datetime, timedelta, UTC
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.domain.enums import LogStatus, TaskPriority, TaskStatus, XpEventType, XpSourceType
from app.models import UserGameProfile, XpRule, XpTransaction
from app.services import habits as habit_svc
from app.services import missions as mission_svc
from app.services.errors import ValidationFailed
from app.services.habits import HabitSpec
from app.services.tasks import change_task_status
from app.services.xp import award_xp, level_for_xp, reconcile_total_xp, total_xp_from_history, xp_required_for_level
from tests.factories import MONDAY, NOW, TODAY, make_habit, make_mission, make_plan, make_task, make_user, run_concurrently


def total(session, user_id):
    session.expire_all()
    return session.execute(select(UserGameProfile.total_xp).where(UserGameProfile.user_id == user_id)).scalar_one()


def tx_count(session, user_id=None):
    stmt = select(func.count()).select_from(XpTransaction)
    if user_id:
        stmt = stmt.where(XpTransaction.user_id == user_id)
    return session.execute(stmt).scalar_one()


# --- idempotent awards -----------------------------------------------------------------------
def test_awarding_the_same_event_twice_gives_xp_once(session, reference_data, user_id):
    task = make_task(session, user_id)
    kw = dict(user_id=user_id, source_type=XpSourceType.TASK, source_id=task.id, rule_code="task.completed.medium", award_date=TODAY)
    first, second = award_xp(session, **kw), award_xp(session, **kw)
    session.commit()
    assert (first.created, second.created) == (True, False)
    assert first.transaction.id == second.transaction.id
    assert tx_count(session) == 1 and total(session, user_id) == 6


def test_a_habit_completed_corrected_and_completed_again_pays_once(session, reference_data, user_id):
    habit = make_habit(session, user_id, spec=HabitSpec(goal_type="quantity", target_value=Decimal("2"), unit="L"))
    a = habit_svc.log_habit(session, user_id, habit.id, TODAY, value=Decimal("2"), now=NOW)  # completed
    habit_svc.log_habit(session, user_id, habit.id, TODAY, value=Decimal("1"), now=NOW)  # corrected down
    c = habit_svc.log_habit(session, user_id, habit.id, TODAY, value=Decimal("2.5"), now=NOW)  # completed again
    session.commit()
    assert a.xp.created and not c.xp.created
    assert tx_count(session) == 1 and total(session, user_id) == 10


def test_partial_and_skipped_logs_give_no_xp(session, reference_data, user_id):
    habit = make_habit(session, user_id, spec=HabitSpec(goal_type="quantity", target_value=Decimal("2"), unit="L"))
    assert habit_svc.log_habit(session, user_id, habit.id, TODAY, value=Decimal("1"), now=NOW).xp is None
    assert habit_svc.log_habit(session, user_id, habit.id, TODAY - timedelta(days=1), skipped=True, now=NOW).xp is None
    session.commit()
    assert tx_count(session) == 0 and total(session, user_id) == 0


def test_mission_completion_pays_once(session, reference_data, user_id):
    mission = make_mission(session, user_id, make_plan(session, user_id))
    a = mission_svc.log_mission(session, user_id, mission.id, MONDAY, status=LogStatus.COMPLETED, now=NOW)
    b = mission_svc.log_mission(session, user_id, mission.id, MONDAY, status=LogStatus.COMPLETED, now=NOW)
    session.commit()
    assert (a.xp.created, b.xp.created) == (True, False)
    assert total(session, user_id) == 15


# --- a task pays once in its whole life --------------------------------------------------------
def test_reopening_and_completing_a_task_again_pays_nothing_more(session, reference_data, user_id):
    task = make_task(session, user_id, priority=TaskPriority.HIGH)
    first = change_task_status(session, user_id, task.id, TaskStatus.DONE, now=NOW)
    session.commit()
    change_task_status(session, user_id, task.id, TaskStatus.TODO, now=NOW)
    again = change_task_status(session, user_id, task.id, TaskStatus.DONE, now=NOW + timedelta(days=3))
    session.commit()
    assert first.xp.created and not again.xp.created
    assert tx_count(session) == 1 and total(session, user_id) == 10  # reopening does not claw back either


def test_requesting_done_on_an_already_done_task_is_a_safe_noop(session, reference_data, user_id):
    task = make_task(session, user_id)
    change_task_status(session, user_id, task.id, TaskStatus.DONE, now=NOW)
    retry = change_task_status(session, user_id, task.id, TaskStatus.DONE, now=NOW)
    session.commit()
    assert retry.xp.created is False and total(session, user_id) == 6


def test_the_database_itself_allows_one_reward_per_task(session, reference_data, user_id):
    task = make_task(session, user_id)
    rule = session.execute(select(XpRule).where(XpRule.rule_code == "task.completed.medium")).scalar_one()
    common = dict(user_id=user_id, xp_rule_id=rule.id, task_id=task.id, amount=6, award_date=TODAY)
    session.add(XpTransaction(idempotency_key="task:A", **common))
    session.commit()
    session.add(XpTransaction(idempotency_key="a-different-key", **common))  # sidesteps the key
    with pytest.raises(IntegrityError):
        session.commit()  # UNIQUE (task_id, event_type) still blocks it
    session.rollback()
    session.add(XpTransaction(idempotency_key="task:A", **{**common, "task_id": make_task(session, user_id).id}))
    with pytest.raises(IntegrityError):
        session.commit()  # UNIQUE (user_id, idempotency_key)


# --- rules and daily caps -----------------------------------------------------------------------
def _rule(session, code, source, amount, cap, since):
    session.add(XpRule(rule_code=code, source_type=source, xp_amount=amount, daily_cap=cap, effective_from=since))
    session.commit()


def test_daily_cap_truncates_then_records_zero_xp(session, user_id):
    _rule(session, "task.completed.medium", "task", 15, 20, date(2026, 1, 1))
    results = []
    for _ in range(3):
        t = make_task(session, user_id)
        results.append(change_task_status(session, user_id, t.id, TaskStatus.DONE, now=NOW))
        session.commit()
    assert [r.xp.transaction.amount for r in results] == [15, 5, 0]
    assert total(session, user_id) == 20
    assert tx_count(session, user_id) == 3  # the zero-XP award is recorded, so it is consumed


def test_the_cap_resets_the_next_local_day(session, user_id):
    _rule(session, "task.completed.medium", "task", 15, 15, date(2026, 1, 1))
    for offset in (0, 1):
        t = make_task(session, user_id)
        change_task_status(session, user_id, t.id, TaskStatus.DONE, now=NOW + timedelta(days=offset))
        session.commit()
    assert total(session, user_id) == 30


def test_the_cap_follows_the_users_timezone(session):
    """15:00 UTC Sunday is already Monday in Auckland: those awards count on a different day."""
    auckland = make_user(session, timezone="Pacific/Auckland")
    _rule(session, "task.completed.medium", "task", 15, 15, date(2026, 1, 1))
    t = make_task(session, auckland)
    r = change_task_status(session, auckland, t.id, TaskStatus.DONE, now=NOW)
    session.commit()
    assert r.xp.transaction.award_date == date(2026, 10, 26)


def test_caps_are_per_rule_and_per_user(session, user_id, other_user_id):
    _rule(session, "task.completed.medium", "task", 15, 15, date(2026, 1, 1))
    _rule(session, "task.completed.high", "task", 15, 15, date(2026, 1, 1))
    for uid, prio in [(user_id, TaskPriority.MEDIUM), (user_id, TaskPriority.HIGH), (other_user_id, TaskPriority.MEDIUM)]:
        t = make_task(session, uid, priority=prio)
        change_task_status(session, uid, t.id, TaskStatus.DONE, now=NOW)
        session.commit()
    assert total(session, user_id) == 30 and total(session, other_user_id) == 15


def test_the_rule_in_force_on_the_award_date_is_used(session, user_id):
    _rule(session, "task.completed.medium", "task", 6, None, date(2026, 1, 1))
    _rule(session, "task.completed.medium", "task", 9, None, date(2026, 10, 26))  # takes effect tomorrow
    t1, t2 = make_task(session, user_id), make_task(session, user_id)
    a = change_task_status(session, user_id, t1.id, TaskStatus.DONE, now=NOW)
    b = change_task_status(session, user_id, t2.id, TaskStatus.DONE, now=NOW + timedelta(days=1))
    session.commit()
    assert (a.xp.transaction.amount, b.xp.transaction.amount) == (6, 9)


def test_missing_or_mismatched_rule_fails_loudly(session, user_id):
    task = make_task(session, user_id)
    with pytest.raises(ValidationFailed, match="no XP rule"):
        award_xp(session, user_id=user_id, source_type=XpSourceType.TASK, source_id=task.id, rule_code="nope", award_date=TODAY)
    _rule(session, "habit.completed", "habit_log", 10, None, date(2026, 1, 1))
    with pytest.raises(ValidationFailed, match="does not apply"):
        award_xp(session, user_id=user_id, source_type=XpSourceType.TASK, source_id=task.id, rule_code="habit.completed", award_date=TODAY)


# --- immutable ledger ----------------------------------------------------------------------------
@pytest.fixture
def one_transaction(session, reference_data, user_id):
    task = make_task(session, user_id)
    change_task_status(session, user_id, task.id, TaskStatus.DONE, now=NOW)
    session.commit()
    return session.execute(select(XpTransaction)).scalar_one()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE xp_transactions SET amount = 9999",
        "UPDATE xp_transactions SET award_date = '2020-01-01'",
        "UPDATE xp_transactions SET idempotency_key = 'rewritten'",
        "DELETE FROM xp_transactions",
    ],
)
def test_ledger_rows_cannot_be_updated_or_deleted(session, one_transaction, sql):
    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(text(sql))
        session.commit()
    session.rollback()
    assert session.execute(select(XpTransaction.amount)).scalar_one() == 6


def test_ledger_rows_cannot_be_modified_through_the_orm_either(session, one_transaction):
    one_transaction.amount = 1
    with pytest.raises(IntegrityError, match="append-only"):
        session.commit()
    session.rollback()
    session.delete(one_transaction)
    with pytest.raises(IntegrityError, match="append-only"):
        session.commit()


# --- the total is rebuildable ----------------------------------------------------------------------
def test_total_always_equals_the_sum_of_the_ledger(session, reference_data, user_id):
    for prio in (TaskPriority.LOW, TaskPriority.MEDIUM, TaskPriority.HIGH):
        t = make_task(session, user_id, priority=prio)
        change_task_status(session, user_id, t.id, TaskStatus.DONE, now=NOW)
    session.commit()
    assert total(session, user_id) == total_xp_from_history(session, user_id) == 3 + 6 + 10


def test_reconcile_detects_and_repairs_a_drifted_cache(session, reference_data, user_id):
    t = make_task(session, user_id)
    change_task_status(session, user_id, t.id, TaskStatus.DONE, now=NOW)
    session.commit()
    session.execute(update(UserGameProfile).where(UserGameProfile.user_id == user_id).values(total_xp=999))
    session.commit()
    assert reconcile_total_xp(session, user_id) == (999, 6)
    session.commit()
    assert total(session, user_id) == 6
    assert reconcile_total_xp(session, user_id) == (6, 6)


def test_an_award_that_fails_leaves_neither_ledger_row_nor_total(session, reference_data, user_id):
    task = make_task(session, user_id)
    with pytest.raises(ValidationFailed):
        award_xp(session, user_id=user_id, source_type=XpSourceType.TASK, source_id=task.id, rule_code="missing", award_date=TODAY)
    session.rollback()
    assert tx_count(session) == 0 and total(session, user_id) == 0


# --- level progression -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "xp, level",
    [(0, 1), (99, 1), (100, 2), (282, 2), (283, 3), (519, 3), (520, 4), (10**9, 100)],
)
def test_level_is_derived_from_total_xp_with_the_default_curve(xp, level):
    assert level_for_xp(xp, Settings()) == level


def test_level_curve_is_configurable_and_monotonic():
    cfg = Settings(level_base_xp=50, level_exponent=2.0, max_level=10)
    assert [xp_required_for_level(n, cfg) for n in (1, 2, 3, 4)] == [0, 50, 200, 450]
    assert level_for_xp(449, cfg) == 3 and level_for_xp(450, cfg) == 4 and level_for_xp(10**7, cfg) == 10
    required = [xp_required_for_level(n, Settings()) for n in range(1, 60)]
    assert required == sorted(required)


# --- concurrency -----------------------------------------------------------------------------------------
def test_simultaneous_completions_of_one_task_pay_exactly_once(session, session_factory, reference_data, user_id):
    task = make_task(session, user_id, priority=TaskPriority.HIGH)

    def work(s, _):
        return change_task_status(s, user_id, task.id, TaskStatus.DONE, now=NOW).xp.created

    created = run_concurrently(session_factory, work, workers=10)
    assert created.count(True) == 1 and created.count(False) == 9
    assert tx_count(session) == 1 and total(session, user_id) == 10


def test_simultaneous_awards_for_the_same_event_pay_exactly_once(session, session_factory, reference_data, user_id):
    """Directly at the XP layer (no task row lock to help): the per-user lock + UNIQUE do it."""
    task = make_task(session, user_id)

    def work(s, _):
        return award_xp(
            s, user_id=user_id, source_type=XpSourceType.TASK, source_id=task.id,
            rule_code="task.completed.medium", award_date=TODAY,
        ).created  # fmt: skip

    created = run_concurrently(session_factory, work, workers=12)
    assert created.count(True) == 1
    assert tx_count(session) == 1 and total(session, user_id) == 6


def test_simultaneous_logs_of_one_habit_day_create_one_row_and_one_reward(session, session_factory, reference_data, user_id):
    habit = make_habit(session, user_id)

    def work(s, _):
        return habit_svc.log_habit(s, user_id, habit.id, TODAY, now=NOW).xp.created

    created = run_concurrently(session_factory, work, workers=10)
    assert created.count(True) == 1
    from app.models import HabitLog

    assert session.execute(select(func.count()).select_from(HabitLog)).scalar_one() == 1
    assert tx_count(session) == 1 and total(session, user_id) == 10


def test_the_daily_cap_holds_under_concurrency(session, session_factory, user_id):
    _rule(session, "task.completed.high", "task", 10, 30, date(2026, 1, 1))
    tasks = [make_task(session, user_id, priority=TaskPriority.HIGH) for _ in range(12)]

    def work(s, i):
        return change_task_status(s, user_id, tasks[i].id, TaskStatus.DONE, now=NOW).xp.transaction.amount

    amounts = run_concurrently(session_factory, work, workers=12)
    assert sum(amounts) == 30 and sorted(amounts, reverse=True)[:3] == [10, 10, 10]  # never above the cap
    assert total(session, user_id) == total_xp_from_history(session, user_id) == 30


def test_concurrent_awards_to_different_users_do_not_interfere(session, session_factory, reference_data):
    users = [make_user(session) for _ in range(6)]
    tasks = [make_task(session, u) for u in users]

    def work(s, i):
        return change_task_status(s, users[i], tasks[i].id, TaskStatus.DONE, now=NOW).xp.created

    assert all(run_concurrently(session_factory, work, workers=6))
    assert all(total(session, u) == 6 for u in users)
