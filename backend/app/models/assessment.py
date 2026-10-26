"""Initial assessment (module D): versioned questionnaire, assessments and answers."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, TimestampMixin, uuid_pk
from app.domain.enums import AnswerType, AssessmentStatus, QuestionDomain, sql_in


class AssessmentVersion(Base, CreatedAtMixin):
    """A published questionnaire. Append-only by convention: publish a new version instead."""

    __tablename__ = "assessment_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))

    questions: Mapped[list[AssessmentQuestion]] = relationship(
        back_populates="assessment_version", order_by="AssessmentQuestion.position"
    )

    __table_args__ = (
        UniqueConstraint("version", name="uq_assessment_versions_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        # Unique over a constant column + partial predicate => at most one active version.
        Index(
            "uq_assessment_versions_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )


class AssessmentQuestion(Base, CreatedAtMixin):
    __tablename__ = "assessment_questions"

    id: Mapped[uuid.UUID] = uuid_pk()
    assessment_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assessment_versions.id", ondelete="RESTRICT"), nullable=False
    )
    question_key: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    answer_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Static question configuration (scale range, choices). Never user data.
    options: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))

    assessment_version: Mapped[AssessmentVersion] = relationship(back_populates="questions")

    __table_args__ = (
        UniqueConstraint("assessment_version_id", "question_key", name="uq_assessment_questions_version_key"),
        UniqueConstraint("assessment_version_id", "position", name="uq_assessment_questions_version_pos"),
        UniqueConstraint("id", "assessment_version_id", name="uq_assessment_questions_id_version"),
        CheckConstraint(sql_in("domain", QuestionDomain), name="domain_valid"),
        CheckConstraint(sql_in("answer_type", AnswerType), name="answer_type_valid"),
        CheckConstraint("position >= 0", name="position_nonneg"),
    )


class Assessment(Base, TimestampMixin):
    __tablename__ = "assessments"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    assessment_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assessment_versions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        default=AssessmentStatus.IN_PROGRESS.value,
        server_default="in_progress",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_assessments_id_user_id"),
        UniqueConstraint("id", "assessment_version_id", name="uq_assessments_id_version"),
        CheckConstraint(sql_in("status", AssessmentStatus), name="status_valid"),
        CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)", name="completed_at_matches_status"
        ),
        Index("ix_assessments_user_id_started_at", "user_id", "started_at"),
        Index("ix_assessments_assessment_version_id", "assessment_version_id"),
        Index(
            "uq_assessments_user_in_progress",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'in_progress'"),
        ),
    )


class AssessmentAnswer(Base, TimestampMixin):
    """One typed answer per (assessment, question). Not a single JSON blob."""

    __tablename__ = "assessment_answers"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assessment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Deliberate denormalization: the composite FKs force it to match both the
    # assessment's version and the question's version.
    assessment_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    question_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    value_number: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    value_text: Mapped[str | None] = mapped_column(Text)
    value_options: Mapped[list[Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        ForeignKeyConstraint(
            ["assessment_id", "user_id"],
            ["assessments.id", "assessments.user_id"],
            name="fk_assessment_answers_assessment_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["assessment_id", "assessment_version_id"],
            ["assessments.id", "assessments.assessment_version_id"],
            name="fk_assessment_answers_assessment_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["question_id", "assessment_version_id"],
            ["assessment_questions.id", "assessment_questions.assessment_version_id"],
            name="fk_assessment_answers_question_in_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("assessment_id", "question_id", name="uq_assessment_answers_assessment_question"),
        CheckConstraint(
            "num_nonnulls(value_number, value_text, value_options) = 1", name="exactly_one_value"
        ),
        Index("ix_assessment_answers_question_id", "question_id"),
        Index("ix_assessment_answers_user_id", "user_id"),
    )
