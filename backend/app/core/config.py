"""Application settings, loaded from environment variables / `.env`.

No secret is hard-coded here. See `backend/.env.example` for the variables.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Atelier"
    environment: str = "development"
    api_v1_prefix: str = "/api/v1"

    # PostgreSQL (SQLAlchemy URL, psycopg 3 driver).
    database_url: str = Field(
        default="postgresql+psycopg://atelier:change-me@localhost:5432/atelier_dev",
        description="Application database. Tests use TEST_DATABASE_URL instead.",
    )

    # Set true behind a transaction-mode pooler (e.g. Supabase pooler on port 6543): server-side
    # prepared statements do not work there. Passed to psycopg as prepare_threshold=None.
    database_disable_prepared_statements: bool = False

    # Supabase Auth: tokens are verified here, in the backend.
    supabase_jwt_issuer: str = ""  # e.g. https://<ref>.supabase.co/auth/v1
    supabase_jwt_audience: str = "authenticated"
    supabase_jwks_url: str = ""  # asymmetric signing keys (ES256/RS256) - preferred
    supabase_jwt_secret: str = ""  # legacy shared secret (HS256) - only if no JWKS

    # How many days back a habit/mission occurrence may be recorded (today counts as day 0).
    # Stops weeks of back-filling just to farm XP or repair a streak.
    max_backfill_days: int = Field(default=7, ge=0)

    # Gamification: level progression (see docs/data-model.md, section 6.3).
    level_base_xp: int = 100
    level_exponent: float = 1.5
    max_level: int = 100

    @field_validator("database_url")
    @classmethod
    def _require_psycopg_driver(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
