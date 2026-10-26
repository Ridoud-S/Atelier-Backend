"""XP awarding: idempotent, capped per day, transactional (docs/data-model.md, section 6)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.domain.enums import XpEventType, XpSourceType
from app.models import UserGameProfile, XpRule, XpTransaction
from app.services.errors import ValidationFailed

_SOURCE_COLUMN = {
    XpSourceType.HABIT_LOG: "habit_log_id",
    XpSourceType.MISSION_LOG: "mission_log_id",
    XpSourceType.TASK: "task_id",
}


@dataclass(frozen=True)
class AwardResult:
    transaction: XpTransaction
    created: bool  # False => the same event was already rewarded (idempotent replay)


def idempotency_key(source_type: XpSourceType, source_id: uuid.UUID, event: XpEventType) -> str:
    return f"{source_type.value}:{source_id}:{event.value}"


def award_xp(
    session: Session,
    *,
    user_id: uuid.UUID,
    source_type: XpSourceType,
    source_id: uuid.UUID,
    rule_code: str,
    award_date: date,
    event_type: XpEventType = XpEventType.COMPLETION,
) -> AwardResult:
    """Reward one event, at most once. Must run inside the caller's transaction.

    Concurrency: the per-user row in `user_game_profiles` is locked (SELECT ... FOR UPDATE)
    first, so concurrent awards for one user are serialized. That makes the replay check and
    the daily-cap computation race-free. UNIQUE constraints remain as the last line of defence.
    """
    key = idempotency_key(source_type, source_id, event_type)

    session.execute(
        pg_insert(UserGameProfile)
        .values(user_id=user_id)
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    session.execute(
        select(UserGameProfile.user_id).where(UserGameProfile.user_id == user_id).with_for_update()
    )

    existing = session.execute(
        select(XpTransaction)
        .where(XpTransaction.user_id == user_id, XpTransaction.idempotency_key == key)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if existing is not None:
        return AwardResult(existing, created=False)

    rule = session.execute(
        select(XpRule)
        .where(XpRule.rule_code == rule_code, XpRule.effective_from <= award_date)
        .order_by(XpRule.effective_from.desc())
        .limit(1)
    ).scalar_one_or_none()
    if rule is None:
        raise ValidationFailed(f"no XP rule '{rule_code}' in force on {award_date}")
    if rule.source_type != source_type.value:
        raise ValidationFailed(f"rule '{rule_code}' does not apply to {source_type.value}")

    amount = rule.xp_amount
    if rule.daily_cap is not None:
        already = session.execute(
            select(func.coalesce(func.sum(XpTransaction.amount), 0))
            .join(XpRule, XpRule.id == XpTransaction.xp_rule_id)
            .where(
                XpTransaction.user_id == user_id,
                XpTransaction.award_date == award_date,
                XpRule.rule_code == rule_code,
            )
        ).scalar_one()
        amount = max(0, min(amount, rule.daily_cap - int(already)))

    transaction = XpTransaction(
        user_id=user_id,
        xp_rule_id=rule.id,
        event_type=event_type.value,
        amount=amount,
        award_date=award_date,
        idempotency_key=key,
        **{_SOURCE_COLUMN[source_type]: source_id},
    )
    session.add(transaction)
    session.flush()  # a violated UNIQUE/FK raises here, before the total changes

    session.execute(
        update(UserGameProfile)
        .where(UserGameProfile.user_id == user_id)
        .values(total_xp=UserGameProfile.total_xp + amount)
    )
    session.flush()
    return AwardResult(transaction, created=True)


def total_xp_from_history(session: Session, user_id: uuid.UUID) -> int:
    return int(
        session.execute(
            select(func.coalesce(func.sum(XpTransaction.amount), 0)).where(
                XpTransaction.user_id == user_id
            )
        ).scalar_one()
    )


def reconcile_total_xp(session: Session, user_id: uuid.UUID) -> tuple[int, int]:
    """Return (stored, rebuilt). If they differ, the cache is rewritten from the ledger."""
    session.execute(
        select(UserGameProfile.user_id).where(UserGameProfile.user_id == user_id).with_for_update()
    )
    stored = int(
        session.execute(
            select(UserGameProfile.total_xp)
            .where(UserGameProfile.user_id == user_id)
            .execution_options(populate_existing=True)
        ).scalar_one()
    )
    rebuilt = total_xp_from_history(session, user_id)
    if stored != rebuilt:
        session.execute(
            update(UserGameProfile).where(UserGameProfile.user_id == user_id).values(total_xp=rebuilt)
        )
        session.flush()
    return stored, rebuilt


def xp_required_for_level(level: int, settings: Settings | None = None) -> int:
    """Cumulative XP needed to reach `level` (level 1 = 0 XP). See docs, section 6.3."""
    cfg = settings or get_settings()
    if level < 1:
        raise ValueError("level must be >= 1")
    return round(cfg.level_base_xp * (level - 1) ** cfg.level_exponent)


def level_for_xp(total_xp: int, settings: Settings | None = None) -> int:
    cfg = settings or get_settings()
    if total_xp < 0:
        raise ValueError("total_xp must be >= 0")
    level = 1
    while level < cfg.max_level and xp_required_for_level(level + 1, cfg) <= total_xp:
        level += 1
    return level
