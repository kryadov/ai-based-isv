"""SQLAlchemy models of the control plane (spec §7.3, §7.7)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project: Mapped[str] = mapped_column(String(120))
    task_ref: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    budget_tokens: Mapped[int] = mapped_column(Integer, default=200_000)
    budget_spent: Mapped[int] = mapped_column(Integer, default=0)


class VertexRow(Base):
    __tablename__ = "vertices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(120))
    deps_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    paths_allow_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    done_when: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    leased_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class IdentityRow(Base):
    __tablename__ = "identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    role: Mapped[str] = mapped_column(String(40), default="executor")
    allowed_sections_json: Mapped[list[str]] = mapped_column(JSON, default=list)


class BudgetRow(Base):
    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject: Mapped[str] = mapped_column(String(120), unique=True)
    limit_tokens: Mapped[int] = mapped_column(Integer, default=0)
    spent_tokens: Mapped[int] = mapped_column(Integer, default=0)


class HITLRow(Base):
    __tablename__ = "hitl_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(200))
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(120))
    target: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(40), default="ok")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MergeQueueRow(Base):
    __tablename__ = "merge_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pr_ref: Mapped[str] = mapped_column(String(120))
    project: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
