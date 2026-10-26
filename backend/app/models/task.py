"""Projects, tasks and checklist items (module C)."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, uuid_pk
from app.domain.enums import ProjectStatus, TaskPriority, TaskStatus, sql_in


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=ProjectStatus.ACTIVE.value, server_default="active"
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_projects_id_user_id"),
        CheckConstraint(sql_in("status", ProjectStatus), name="status_valid"),
        CheckConstraint(
            "(status = 'archived') = (archived_at IS NOT NULL)", name="archived_at_matches_status"
        ),
        Index("ix_projects_user_id_status", "user_id", "status"),
        Index(
            "uq_projects_user_open_name",
            "user_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("status <> 'archived'"),
        ),
    )


class Task(Base, TimestampMixin):
    """One-off task, optionally inside a project of the same user."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(
        String(10), nullable=False, default=TaskPriority.MEDIUM.value, server_default="medium"
    )
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=TaskStatus.TODO.value, server_default="todo"
    )
    scheduled_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    checklist_items: Mapped[list[TaskChecklistItem]] = relationship(
        back_populates="task",
        order_by="TaskChecklistItem.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # project_id is NULL for standalone tasks; MATCH SIMPLE then skips this FK.
        ForeignKeyConstraint(
            ["project_id", "user_id"],
            ["projects.id", "projects.user_id"],
            name="fk_tasks_project_owner",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("id", "user_id", name="uq_tasks_id_user_id"),
        CheckConstraint(sql_in("priority", TaskPriority), name="priority_valid"),
        CheckConstraint(sql_in("status", TaskStatus), name="status_valid"),
        CheckConstraint(
            "(status = 'done') = (completed_at IS NOT NULL)", name="completed_at_matches_status"
        ),
        CheckConstraint(
            "due_date IS NULL OR scheduled_date IS NULL OR due_date >= scheduled_date",
            name="due_after_scheduled",
        ),
        Index("ix_tasks_user_id_scheduled_date", "user_id", "scheduled_date"),
        Index("ix_tasks_user_id_status", "user_id", "status"),
        Index("ix_tasks_project_id", "project_id"),
    )


class TaskChecklistItem(Base, TimestampMixin):
    """Dependent part of a task (CASCADE). Gives no XP by itself."""

    __tablename__ = "task_checklist_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    is_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    task: Mapped[Task] = relationship(back_populates="checklist_items")

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "position",
            name="uq_task_checklist_items_task_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("position >= 0", name="position_nonneg"),
    )
