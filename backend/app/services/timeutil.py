"""Local-date helpers. Occurrences are `date`s in the user's timezone (see docs, section 8)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.errors import ValidationFailed


def validate_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValidationFailed(f"unknown timezone: {name!r}") from exc


def local_today(timezone: str, now: datetime | None = None) -> date:
    """Today's date for a user in `timezone`. `now` (aware) is injectable for tests."""
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    return instant.astimezone(validate_timezone(timezone)).date()


def week_start_for(day: date, week_start_day: int = 1) -> date:
    """First day of the week containing `day`. `week_start_day` is ISO (1=Mon .. 7=Sun)."""
    if not 1 <= week_start_day <= 7:
        raise ValidationFailed("week_start_day must be between 1 and 7")
    return day - timedelta(days=(day.isoweekday() - week_start_day) % 7)


def assert_loggable_date(occurrence_date: date, today: date, max_backfill_days: int) -> None:
    """An occurrence may be recorded for today or up to `max_backfill_days` days back."""
    if occurrence_date > today:
        raise ValidationFailed("occurrence_date cannot be in the future (user's local date)")
    if (today - occurrence_date).days > max_backfill_days:
        raise ValidationFailed(
            f"occurrence_date is more than {max_backfill_days} days in the past; "
            "older days can no longer be recorded"
        )
