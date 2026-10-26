"""Reference data: XP rules, questionnaire v1 and habit templates.

Idempotent (INSERT ... ON CONFLICT DO NOTHING on natural keys), so it is safe to re-run.
Kept out of the Alembic migration on purpose: it is configuration, not structure.

    python -m app.db.seed
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.session import make_session_factory, get_engine
from app.models import AssessmentQuestion, AssessmentVersion, HabitTemplate, XpRule

RULES_EFFECTIVE_FROM = date(2026, 1, 1)

# (rule_code, source_type, xp_amount, daily_cap)
XP_RULES = [
    ("habit.completed", "habit_log", 10, 100),
    ("mission.completed", "mission_log", 15, 75),
    ("task.completed.low", "task", 3, 30),
    ("task.completed.medium", "task", 6, 60),
    ("task.completed.high", "task", 10, 100),
]

_SCALE_1_5 = {"min": 1, "max": 5}

# (key, domain, prompt, answer_type, options, required)
QUESTIONS_V1 = [
    ("sleep_hours", "sleep", "¿Cuántas horas duermes en promedio por noche?", "number", {"min": 0, "max": 14}, True),
    ("sleep_regularity", "sleep", "¿Qué tan regular es tu horario de sueño? (1 = nada, 5 = muy regular)", "scale", _SCALE_1_5, True),
    ("hydration_glasses", "hydration", "¿Cuántos vasos de agua tomas al día?", "number", {"min": 0, "max": 30}, True),
    ("nutrition_planning", "nutrition", "¿Qué tan organizadas tienes tus comidas? (1 = nada, 5 = muy organizadas)", "scale", _SCALE_1_5, True),
    ("activity_days", "activity", "¿Cuántos días a la semana haces actividad física?", "number", {"min": 0, "max": 7}, True),
    ("focus_distractions", "focus", "¿Qué tanto te distraes cuando necesitas concentrarte? (1 = poco, 5 = mucho)", "scale", _SCALE_1_5, True),
    ("energy_level", "wellbeing", "¿Cómo describirías tu energía y bienestar general? (1 = bajo, 5 = alto)", "scale", _SCALE_1_5, True),
    (
        "improve_areas",
        "goals",
        "¿Qué hábitos quieres mejorar?",
        "multi_choice",
        {"choices": ["sleep", "hydration", "nutrition", "activity", "focus", "wellbeing"]},
        True,
    ),
    ("time_available_minutes", "availability", "¿Cuántos minutos al día puedes dedicar a nuevas rutinas?", "number", {"min": 0, "max": 480}, True),
]

# (code, title, category, goal_type, target, unit, weekly_target, difficulty)
TEMPLATES = [
    ("drink_water", "Tomar agua", "hydration", "quantity", Decimal("8"), "glasses", 7, 1),
    ("fixed_bedtime", "Acostarte a tu hora", "sleep", "binary", None, None, 5, 2),
    ("daily_walk", "Caminata diaria", "activity", "duration", Decimal("20"), "minutes", 4, 2),
    ("plan_meals", "Planear las comidas de la semana", "nutrition", "binary", None, None, 1, 2),
    ("deep_work_block", "Bloque de trabajo enfocado", "focus", "duration", Decimal("25"), "minutes", 3, 3),
    ("evening_review", "Revisión breve del día", "wellbeing", "binary", None, None, 5, 1),
]


def seed_reference_data(session: Session) -> None:
    for code, source_type, amount, cap in XP_RULES:
        session.execute(
            pg_insert(XpRule)
            .values(
                rule_code=code,
                effective_from=RULES_EFFECTIVE_FROM,
                source_type=source_type,
                xp_amount=amount,
                daily_cap=cap,
            )
            .on_conflict_do_nothing(constraint="uq_xp_rules_code_from")
        )

    session.execute(
        pg_insert(AssessmentVersion)
        .values(version=1, title="Evaluación inicial v1")
        .on_conflict_do_nothing(constraint="uq_assessment_versions_version")
    )
    version = session.execute(select(AssessmentVersion).where(AssessmentVersion.version == 1)).scalar_one()
    for position, (key, domain, prompt, answer_type, options, required) in enumerate(QUESTIONS_V1):
        session.execute(
            pg_insert(AssessmentQuestion)
            .values(
                assessment_version_id=version.id,
                question_key=key,
                domain=domain,
                prompt=prompt,
                answer_type=answer_type,
                options=options,
                position=position,
                is_required=required,
            )
            .on_conflict_do_nothing(constraint="uq_assessment_questions_version_key")
        )
    has_active = session.execute(
        select(AssessmentVersion.id).where(AssessmentVersion.is_active.is_(True))
    ).first()
    if has_active is None:
        session.execute(
            update(AssessmentVersion).where(AssessmentVersion.id == version.id).values(is_active=True)
        )

    for code, title, category, goal, target, unit, weekly, difficulty in TEMPLATES:
        session.execute(
            pg_insert(HabitTemplate)
            .values(
                code=code,
                version=1,
                title=title,
                category=category,
                goal_type=goal,
                target_value=target,
                unit=unit,
                default_weekly_target_count=weekly,
                difficulty=difficulty,
            )
            .on_conflict_do_nothing(constraint="uq_habit_templates_code_version")
        )
    session.flush()


def main() -> None:
    factory = make_session_factory(get_engine())
    with factory() as session:
        seed_reference_data(session)
        session.commit()
    print("Reference data seeded.")


if __name__ == "__main__":
    main()
