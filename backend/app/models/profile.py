"""Users and settings (module A) and the per-user game summary (module E)."""
from __future__ import annotations

import uuid
from datetime import time

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    SmallInteger,
    String,
    Time,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.domain.enums import Theme, TimeFormat, sql_in


class Profile(Base, TimestampMixin):
    """One row per Supabase Auth user. `id` is the token's `sub`; no passwords here."""

    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(100))


class UserSettings(Base, TimestampMixin):
    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), primary_key=True
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC", default="UTC")
    language: Mapped[str] = mapped_column(String(10), nullable=False, server_default="es", default="es")
    theme: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=Theme.SYSTEM.value, default=Theme.SYSTEM.value
    )
    week_start_day: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1", default=1)
    time_format: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=TimeFormat.H24.value, default=TimeFormat.H24.value
    )
    notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    daily_reminder_time: Mapped[time | None] = mapped_column(Time)

    __table_args__ = (
        CheckConstraint(sql_in("theme", Theme), name="theme_valid"),
        CheckConstraint(sql_in("time_format", TimeFormat), name="time_format_valid"),
        CheckConstraint("week_start_day BETWEEN 1 AND 7", name="week_start_day_range"),
        CheckConstraint("language ~ '^[a-z]{2}(-[A-Z]{2})?$'", name="language_format"),
    )


class UserGameProfile(Base, TimestampMixin):
    """Cache of SUM(xp_transactions.amount). The level is derived, never stored."""

    __tablename__ = "user_game_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), primary_key=True
    )
    total_xp: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0", default=0)

    __table_args__ = (CheckConstraint("total_xp >= 0", name="total_xp_nonneg"),)
