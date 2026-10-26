"""Ownership-scoped lookups. Every read of a user's entity goes through here."""
from __future__ import annotations

import uuid
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.errors import NotFoundError

T = TypeVar("T")


def get_owned(
    session: Session,
    model: type[T],
    entity_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> T:
    """Return the entity only if it belongs to `user_id`.

    A foreign or non-existent id raises the same NotFoundError, so a caller cannot probe
    which ids exist for other users (the API answers 404 in both cases).
    """
    stmt = select(model).where(model.id == entity_id, model.user_id == user_id)  # type: ignore[attr-defined]
    if for_update:
        stmt = stmt.with_for_update()
    stmt = stmt.execution_options(populate_existing=True)
    entity = session.execute(stmt).scalar_one_or_none()
    if entity is None:
        raise NotFoundError(f"{model.__name__} not found")
    return entity
