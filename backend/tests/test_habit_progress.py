"""Weekly progress (docs/data-model.md, section 5), with the Mon 2026-10-19 .. Sun 10-25 week."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.domain.enums import FrequencyType, GoalType
from app.services import habits as svc
from app.services.habits import HabitSpec
from app.services.progress import weekly_progress
from tests.factories import MONDAY, NOW, make_habit

D = lambda n: MONDAY + timedelta(days=n)  # noqa: E731  (0 = Mon ... 6 = Sun)
TPW3 = HabitSpec(frequency_type=FrequencyType.TIMES_PER_WEEK, weekly_target_count=3)


def complete(session, user_id, habit, *days, **kw):
    for n in days:
        svc.log_habit(session, user_id, habit.id, D(n), now=NOW, **kw)
    session.commit()


def test_full_week_target_is_the_weekly_count(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=TPW3)
    complete(session, user_id, habit, 0, 2)
    p = weekly_progress(session, user_id, habit.id, MONDAY)
    assert (p.eligible_days, p.target, p.completed, p.extra) == (7, 3, 2, 0)
    assert p.progress_ratio == pytest.approx(2 / 3) and p.compliance_pct == pytest.approx(66.67)


def test_compliance_is_capped_at_100_but_extra_progress_is_shown(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=TPW3)
    complete(session, user_id, habit, 0, 1, 2, 3, 4)
    p = weekly_progress(session, user_id, habit.id, MONDAY)
    assert p.completed == 5 and p.target == 3
    assert p.progress_ratio == pytest.approx(5 / 3)  # uncapped
    assert p.compliance_pct == 100.0  # capped
    assert p.extra == 2


def test_only_completed_logs_on_distinct_dates_count(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=TPW3)
    complete(session, user_id, habit, 0)
    svc.log_habit(session, user_id, habit.id, D(1), skipped=True, now=NOW)
    svc.log_habit(session, user_id, habit.id, D(0), now=NOW)  # same day again: still one
    session.commit()
    p = weekly_progress(session, user_id, habit.id, MONDAY)
    assert p.completed == 1 and p.target == 3  # skipped does not count nor reduce the target


def test_quantity_partial_does_not_count_as_a_valid_completion(session, reference_data, user_id):
    spec = HabitSpec(goal_type=GoalType.QUANTITY, target_value=Decimal("2"), unit="L",
                     frequency_type=FrequencyType.TIMES_PER_WEEK, weekly_target_count=2)  # fmt: skip
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=spec)
    svc.log_habit(session, user_id, habit.id, D(0), value=Decimal("1.5"), now=NOW)  # partial
    svc.log_habit(session, user_id, habit.id, D(1), value=Decimal("2"), now=NOW)  # completed
    session.commit()
    assert weekly_progress(session, user_id, habit.id, MONDAY).completed == 1


def test_partial_week_when_the_habit_starts_mid_week_is_prorated(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=D(3), spec=TPW3)  # starts Thursday: 4 eligible days
    p = weekly_progress(session, user_id, habit.id, MONDAY)
    assert p.eligible_days == 4
    assert p.target == 2  # ceil(3 * 4 / 7) = ceil(1.71)
    complete(session, user_id, habit, 3, 5)
    assert weekly_progress(session, user_id, habit.id, MONDAY).compliance_pct == 100.0


def test_a_configuration_change_mid_week_blends_both_versions(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1),
                       spec=HabitSpec(frequency_type=FrequencyType.TIMES_PER_WEEK, weekly_target_count=2))  # fmt: skip
    svc.add_version(session, user_id, habit.id, effective_from=D(3),
                    spec=HabitSpec(frequency_type=FrequencyType.TIMES_PER_WEEK, weekly_target_count=5))  # fmt: skip
    session.commit()
    p = weekly_progress(session, user_id, habit.id, MONDAY)
    assert p.target == 4  # ceil(2*3/7 + 5*4/7) = ceil(3.71)


def test_target_never_exceeds_the_days_the_habit_existed(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=D(5),  # Saturday: 2 days
                       spec=HabitSpec(frequency_type=FrequencyType.TIMES_PER_WEEK, weekly_target_count=7))  # fmt: skip
    p = weekly_progress(session, user_id, habit.id, MONDAY)
    assert (p.eligible_days, p.target) == (2, 2)


def test_week_before_the_habit_existed_has_no_progress(session, user_id):
    habit = make_habit(session, user_id, start=D(7), spec=TPW3)  # starts next Monday
    assert weekly_progress(session, user_id, habit.id, MONDAY) is None


def test_week_after_the_end_date_has_no_progress_and_the_end_week_is_truncated(session, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1), spec=TPW3, end_date=D(2))
    assert weekly_progress(session, user_id, habit.id, MONDAY).eligible_days == 3
    assert weekly_progress(session, user_id, habit.id, D(7)) is None


def test_daily_habit_expects_every_day(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1))
    complete(session, user_id, habit, 0, 1, 2, 3)
    p = weekly_progress(session, user_id, habit.id, MONDAY)
    assert (p.target, p.completed, p.compliance_pct) == (7, 4, pytest.approx(57.14))


def test_weekdays_habit_counts_only_scheduled_days_and_reports_the_rest_as_extra(session, reference_data, user_id):
    habit = make_habit(session, user_id, start=date(2026, 10, 1),
                       spec=HabitSpec(frequency_type=FrequencyType.WEEKDAYS, weekdays=(1, 3, 5)))  # Mon/Wed/Fri  # fmt: skip
    complete(session, user_id, habit, 0, 2, 5)  # Mon, Wed, Sat(unscheduled)
    p = weekly_progress(session, user_id, habit.id, MONDAY)
    assert p.target == 3 and p.completed == 2 and p.extra == 1
    assert p.compliance_pct == pytest.approx(66.67)


def test_other_users_habit_has_no_progress_for_me(session, other_user_id, user_id):
    from app.services.errors import NotFoundError

    habit = make_habit(session, other_user_id, spec=TPW3)
    with pytest.raises(NotFoundError):
        weekly_progress(session, user_id, habit.id, MONDAY)
