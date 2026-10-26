"""XP rules and the append-only XP ledger (module E)."""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, uuid_pk
from app.domain.enums import XpEventType, XpSourceType, sql_in


class XpRule(Base, CreatedAtMixin):
    """Reward rule versioned by date. Changed by inserting a row with a later `effective_from`."""

    __tablename__ = "xp_rules"

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_code: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    source_type: Mapped[str] = mapped_column(String(12), nullable=False)
    xp_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    # Max XP per user per local day for this rule_code. NULL = no cap.
    daily_cap: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("rule_code", "effective_from", name="uq_xp_rules_code_from"),
        CheckConstraint(sql_in("source_type", XpSourceType), name="source_type_valid"),
        CheckConstraint("xp_amount >= 0", name="xp_amount_nonneg"),
        CheckConstraint("daily_cap IS NULL OR daily_cap >= 0", name="daily_cap_nonneg"),
    )


class XpTransaction(Base, CreatedAtMixin):
    """Immutable ledger row (UPDATE/DELETE are rejected by a trigger)."""

    __tablename__ = "xp_transactions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    xp_rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("xp_rules.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=XpEventType.COMPLETION.value, server_default="completion"
    )
    # Exactly one source. Three real FKs instead of a polymorphic (type, id) pair.
    habit_log_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    mission_log_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    award_date: Mapped[date] = mapped_column(Date, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["habit_log_id", "user_id"],
            ["habit_logs.id", "habit_logs.user_id"],
            name="fk_xp_transactions_habit_log_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["mission_log_id", "user_id"],
            ["mission_logs.id", "mission_logs.user_id"],
            name="fk_xp_transactions_mission_log_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["task_id", "user_id"],
            ["tasks.id", "tasks.user_id"],
            name="fk_xp_transactions_task_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_xp_transactions_user_key"),
        UniqueConstraint("habit_log_id", "event_type", name="uq_xp_transactions_habit_log_event"),
        UniqueConstraint("mission_log_id", "event_type", name="uq_xp_transactions_mission_log_event"),
        UniqueConstraint("task_id", "event_type", name="uq_xp_transactions_task_event"),
        CheckConstraint(sql_in("event_type", XpEventType), name="event_type_valid"),
        CheckConstraint(
            "num_nonnulls(habit_log_id, mission_log_id, task_id) = 1", name="exactly_one_source"
        ),
        CheckConstraint("amount >= 0", name="amount_nonneg"),
        Index("ix_xp_transactions_user_id_award_date", "user_id", "award_date"),
        Index("ix_xp_transactions_user_id_created_at", "user_id", "created_at"),
        Index("ix_xp_transactions_xp_rule_id", "xp_rule_id"),
    )
