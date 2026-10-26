"""Configuration failures must be loud and early."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import get_token_verifier
from app.core.config import get_settings
from app.db.session import make_engine
from app.main import create_app


@pytest.fixture
def no_auth_config(monkeypatch):
    for name in ("SUPABASE_JWKS_URL", "SUPABASE_JWT_SECRET", "SUPABASE_JWT_ISSUER"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    get_token_verifier.cache_clear()
    yield
    get_settings.cache_clear()
    get_token_verifier.cache_clear()


def test_the_server_refuses_to_start_without_token_verification_config(no_auth_config):
    with pytest.raises(RuntimeError, match="SUPABASE_JWKS_URL"):
        with TestClient(create_app()):
            pass


def test_startup_is_fine_when_a_verifier_is_provided(no_auth_config):
    app = create_app()
    app.dependency_overrides[get_token_verifier] = lambda: object()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_the_engine_can_disable_prepared_statements_for_transaction_poolers(database_url):
    for flag in (False, True):
        engine = make_engine(database_url, disable_prepared_statements=flag)
        with engine.connect() as conn:
            for _ in range(8):  # beyond psycopg's default auto-prepare threshold (5)
                assert conn.execute(text("SELECT 1")).scalar_one() == 1
        engine.dispose()


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_DISABLE_PREPARED_STATEMENTS", "true")
    get_settings.cache_clear()
    try:
        assert get_settings().database_disable_prepared_statements is True
    finally:
        get_settings.cache_clear()
