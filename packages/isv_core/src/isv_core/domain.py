"""Доменные модели и перечисления платформы (спека §7.3, §7.7).

Чистые pydantic-модели без зависимостей от БД/FastAPI.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"


class VertexStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


class HITLStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class DecisionAction(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class HITLKind(StrEnum):
    PLAN_APPROVAL = "plan-approval"
    MERGE = "merge"
    CANARY_PROMOTION = "canary-promotion"
    MEMORY_PROMOTION = "memory-promotion"
    ESCALATION = "escalation"
    AUTOMATION_ACTION = "automation-action"


class TaskVertex(BaseModel):
    """Вершина задачи роя: единица исполнения с изоляцией путей и критерием завершения."""

    id: int | None = None
    run_id: int
    name: str
    deps: list[str] = Field(default_factory=list)
    paths_allow: list[str] = Field(default_factory=list)
    done_when: str
    status: VertexStatus = VertexStatus.QUEUED
    leased_by: str | None = None
    lease_until: datetime | None = None
    attempts: int = 0
    result: dict[str, Any] | None = None


class Run(BaseModel):
    """Прогон: заявка в трекере → полный конвейер до выката."""

    id: int | None = None
    project: str
    task_ref: str
    status: RunStatus = RunStatus.PENDING
    budget_tokens: int = Field(default=200_000, ge=0)
    budget_spent: int = Field(default=0, ge=0)


class AgentIdentity(BaseModel):
    """Идентичность агента (service identity): кто что может делать."""

    id: int | None = None
    name: str
    role: str = "executor"
    allowed_sections: list[str] = Field(default_factory=list)


class BudgetRecord(BaseModel):
    """Бюджет агента/рана: токены и деньги; списание только в пределах остатка."""

    id: int | None = None
    subject: str
    limit_tokens: int = Field(ge=0)
    spent_tokens: int = Field(default=0, ge=0)


class HITLRequest(BaseModel):
    """Запрос решения человека (спека §7.7): рой стоит, пока не решено."""

    id: int | None = None
    kind: HITLKind
    subject: str
    context: dict[str, Any] = Field(default_factory=dict)
    status: HITLStatus = HITLStatus.PENDING
    decided_by: str | None = None
    decision_note: str | None = None
    created_at: datetime | None = None
    decided_at: datetime | None = None


class AuditEvent(BaseModel):
    """Событие audit-журнала (append-only): кто/что/над чем/итог."""

    id: int | None = None
    ts: datetime | None = None
    actor: str
    action: str
    target: str
    outcome: str = "ok"
    payload: dict[str, Any] = Field(default_factory=dict)
