"""Test infrastructure.

Safety: the suite only runs against a database whose name ends in `_test` and that is given
explicitly through TEST_DATABASE_URL. The schema is built by running the real Alembic
migration (never `metadata.create_all`), so the tests exercise exactly what production gets.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.seed import seed_reference_data

# The suite uses fixed historical dates; the real default (7) is tested explicitly.
os.environ.setdefault("MAX_BACKFILL_DAYS", "365")

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.exit(
            "TEST_DATABASE_URL is not set. Point it at an isolated database whose name ends "
            "in '_test' (see docs/database-setup.md).",
            returncode=2,
        )
    assert_safe_database(url)
    return url


def assert_safe_database(url: str) -> None:
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        pytest.exit(f"Refusing to run destructive tests on database '{name}': name must end in '_test'.", returncode=2)


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def database_url() -> str:
    return _test_database_url()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    engine = create_engine(database_url, pool_size=10, max_overflow=20, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(database_url), "head")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


_TABLES = ", ".join(sorted(Base.metadata.tables))


@pytest.fixture
def session(engine: Engine, session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        try:
            yield s
        finally:
            s.rollback()
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {_TABLES} RESTART IDENTITY CASCADE"))


@pytest.fixture
def reference_data(session: Session) -> None:
    seed_reference_data(session)
    session.commit()


@pytest.fixture
def user_id(session: Session) -> uuid.UUID:
    from tests.factories import make_user

    return make_user(session)


@pytest.fixture
def other_user_id(session: Session) -> uuid.UUID:
    from tests.factories import make_user

    return make_user(session)
