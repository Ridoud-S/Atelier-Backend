"""Mission logging (module D). Plan generation / adaptation is out of scope for the V1 schema."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.domain.enums import LogStatus, MissionStatus, XpSourceType
from app.models import Mission, MissionLog, UserSettings
from app.services.errors import ConflictError, ValidationFailed
from app.services.ownership import get_owned
from app.core.config import get_settings
from app.services.timeutil import assert_loggable_date, local_today
from app.services.xp import AwardResult, award_xp

MISSION_XP_RULE = "mission.completed"


@dataclass(frozen=True)
class MissionLogResult:
    log: MissionLog
    xp: AwardResult | None


def log_mission(
    session: Session,
    user_id: uuid.UUID,
    mission_id: uuid.UUID,
    occurrence_date: date,
    *,
    status: LogStatus,
    value: Decimal | None = None,
    perceived_difficulty: int | None = None,
    now: datetime | None = None,
    max_backfill_days: int | None = None,
) -> MissionLogResult:
    mission = get_owned(session, Mission, mission_id, user_id)
    if mission.status != MissionStatus.ACTIVE:
        raise ConflictError("only active missions can be logged")
    if not mission.week_start <= occurrence_date <= mission.week_start + timedelta(days=6):
        raise ValidationFailed("occurrence_date is outside the mission's week")
    if status == LogStatus.SKIPPED:
        value = None

    timezone = session.execute(
        select(UserSettings.timezone).where(UserSettings.user_id == user_id)
    ).scalar_one_or_none() or "UTC"
    today = local_today(timezone, now)
    limit = get_settings().max_backfill_days if max_backfill_days is None else max_backfill_days
    assert_loggable_date(occurrence_date, today, limit)

    stmt = (
        pg_insert(MissionLog)
        .values(
            user_id=user_id,
            mission_id=mission.id,
            occurrence_date=occurrence_date,
            status=status.value,
            value=value,
            perceived_difficulty=perceived_difficulty,
        )
        .on_conflict_do_update(
            constraint="uq_mission_logs_mission_date",
            set_={
                "status": status.value,
                "value": value,
                "perceived_difficulty": perceived_difficulty,
                "updated_at": func.now(),
            },
        )
        .returning(MissionLog.id)
    )
    log_id = session.execute(stmt).scalar_one()
    log = session.execute(
        select(MissionLog).where(MissionLog.id == log_id).execution_options(populate_existing=True)
    ).scalar_one()

    xp = None
    if status == LogStatus.COMPLETED:
        xp = award_xp(
            session,
            user_id=user_id,
            source_type=XpSourceType.MISSION_LOG,
            source_id=log.id,
            rule_code=MISSION_XP_RULE,
            award_date=today,
        )
    return MissionLogResult(log=log, xp=xp)
