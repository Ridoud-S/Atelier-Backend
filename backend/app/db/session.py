"""Engine and session factory."""
from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def make_engine(
    url: str | None = None, *, disable_prepared_statements: bool | None = None, **kwargs
) -> Engine:
    settings = get_settings()
    if disable_prepared_statements is None:
        disable_prepared_statements = settings.database_disable_prepared_statements
    connect_args = dict(kwargs.pop("connect_args", {}))
    if disable_prepared_statements:
        connect_args["prepare_threshold"] = None  # psycopg 3: never auto-prepare
    return create_engine(
        url or settings.database_url,
        pool_pre_ping=True,
        future=True,
        connect_args=connect_args,
        **kwargs,
    )


@lru_cache
def get_engine() -> Engine:
    return make_engine()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: objects stay readable after the request commits.
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request; the endpoint decides when to commit."""
    factory = make_session_factory(get_engine())
    with factory() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
