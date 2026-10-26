"""First-access provisioning of the rows every user needs."""
from __future__ import annotations

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Profile, UserGameProfile, UserSettings


def ensure_profile(session: Session, user_id: uuid.UUID, display_name: str | None = None) -> None:
    """Idempotent and race-safe (INSERT ... ON CONFLICT DO NOTHING)."""
    session.execute(
        pg_insert(Profile)
        .values(id=user_id, display_name=display_name)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    session.execute(
        pg_insert(UserSettings).values(user_id=user_id).on_conflict_do_nothing(index_elements=["user_id"])
    )
    session.execute(
        pg_insert(UserGameProfile)
        .values(user_id=user_id)
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    session.flush()
