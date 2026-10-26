"""Users, profiles, settings and the game profile."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.models import Profile, UserGameProfile, UserSettings
from app.services.errors import ValidationFailed
from app.services.provisioning import ensure_profile
from app.services.timeutil import local_today, validate_timezone, week_start_for
from tests.factories import NOW, make_user


def test_provisioning_creates_profile_settings_and_game_profile_with_defaults(session):
    uid = uuid.uuid4()
    ensure_profile(session, uid, display_name="Ana")
    session.commit()

    profile = session.get(Profile, uid)
    settings = session.get(UserSettings, uid)
    game = session.get(UserGameProfile, uid)
    assert profile.display_name == "Ana"
    assert (settings.timezone, settings.language, settings.theme) == ("UTC", "es", "system")
    assert (settings.week_start_day, settings.time_format, settings.notifications_enabled) == (1, "24h", True)
    assert game.total_xp == 0


def test_provisioning_is_idempotent(session):
    uid = uuid.uuid4()
    for _ in range(3):
        ensure_profile(session, uid)
        session.commit()
    assert session.execute(select(text("count(*)")).select_from(Profile)).scalar_one() == 1


def test_profile_table_stores_no_credentials(engine):
    """Passwords/e-mails live in Supabase Auth, not in application tables."""
    with engine.connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'profiles'")
            )
        }
    assert cols == {"id", "display_name", "created_at", "updated_at"}


def test_settings_and_game_profile_are_one_to_one(session, user_id):
    session.add(UserSettings(user_id=user_id))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add(UserGameProfile(user_id=user_id))
    with pytest.raises(IntegrityError):
        session.commit()


def test_settings_for_unknown_user_are_rejected(session):
    session.add(UserSettings(user_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        session.commit()


@pytest.mark.parametrize(
    "column, bad_value",
    [
        ("theme", "neon"),
        ("time_format", "13h"),
        ("week_start_day", 0),
        ("week_start_day", 8),
        ("language", "EN"),
        ("language", "english"),
    ],
)
def test_invalid_settings_values_are_rejected(session, user_id, column, bad_value):
    with pytest.raises(IntegrityError):
        session.execute(update(UserSettings).where(UserSettings.user_id == user_id).values({column: bad_value}))
        session.commit()


@pytest.mark.parametrize("language", ["es", "en", "pt-BR", "es-MX"])
def test_valid_languages_are_accepted(session, user_id, language):
    session.execute(update(UserSettings).where(UserSettings.user_id == user_id).values(language=language))
    session.commit()


def test_negative_total_xp_is_rejected(session, user_id):
    with pytest.raises(IntegrityError):
        session.execute(update(UserGameProfile).where(UserGameProfile.user_id == user_id).values(total_xp=-1))
        session.commit()


def test_timezone_validation_in_backend():
    assert validate_timezone("America/Mexico_City").key == "America/Mexico_City"
    with pytest.raises(ValidationFailed):
        validate_timezone("Mars/Olympus_Mons")


def test_local_today_follows_the_users_timezone():
    # 2026-10-25 15:00 UTC is already the 26th in Auckland and still the 25th in Mexico City.
    assert local_today("UTC", NOW).isoformat() == "2026-10-25"
    assert local_today("Pacific/Auckland", NOW).isoformat() == "2026-10-26"
    assert local_today("America/Mexico_City", NOW).isoformat() == "2026-10-25"


def test_week_start_respects_user_preference():
    from datetime import date

    sunday = date(2026, 10, 25)
    assert week_start_for(sunday, 1) == date(2026, 10, 19)  # Monday-based
    assert week_start_for(sunday, 7) == date(2026, 10, 25)  # Sunday-based
    assert week_start_for(date(2026, 10, 24), 7) == date(2026, 10, 18)


def test_users_are_independent(session):
    a, b = make_user(session), make_user(session)
    assert a != b
    assert session.execute(select(text("count(*)")).select_from(UserSettings)).scalar_one() == 2
