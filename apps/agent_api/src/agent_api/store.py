"""Store: операции control plane над таблицами (спека §7.3).

Все функции принимают AsyncSession; claim вершин — атомарный
(status-фильтр в UPDATE + RETURNING; на Postgres добавляется SKIP LOCKED).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_api.models import (
    AuditRow,
    BudgetRow,
    HITLRow,
    IdentityRow,
    MergeQueueRow,
    RunRow,
    VertexRow,
)


def _now() -> datetime:
    return datetime.now(UTC)


async def create_run(session: AsyncSession, project: str, task_ref: str,
                     budget_tokens: int = 200_000) -> int:
    row = RunRow(project=project, task_ref=task_ref, budget_tokens=budget_tokens)
    session.add(row)
    await session.flush()
    return row.id


async def add_vertex(session: AsyncSession, run_id: int, name: str, done_when: str,
                     deps: list[str] | None = None,
                     paths_allow: list[str] | None = None) -> int:
    row = VertexRow(run_id=run_id, name=name, done_when=done_when,
                    deps_json=deps or [], paths_allow_json=paths_allow or [])
    session.add(row)
    await session.flush()
    return row.id


async def _deps_satisfied(session: AsyncSession, run_id: int, deps: list[str]) -> bool:
    if not deps:
        return True
    rows = await session.scalars(
        select(VertexRow).where(VertexRow.run_id == run_id, VertexRow.name.in_(deps))
    )
    by_name = {r.name: r.status for r in rows}
    return all(by_name.get(d) == "done" for d in deps)


async def claim_vertex(session: AsyncSession, run_id: int, agent: str,
                       ttl_seconds: int = 300) -> dict[str, Any] | None:
    """Атомарно взять queued-вершину с выполненными deps (или истёкшим lease)."""
    rows = await session.scalars(
        select(VertexRow).where(
            VertexRow.run_id == run_id,
            VertexRow.status.in_(["queued", "running"]),
        )
    )
    now = _now()
    candidate: VertexRow | None = None
    for r in rows:
        expired = r.lease_until is not None and r.lease_until < now
        if r.status == "leased" and not expired:
            continue
        if r.status == "running":
            continue
        if not await _deps_satisfied(session, run_id, list(r.deps_json or [])):
            continue
        candidate = r
        break
    if candidate is None:
        return None
    result = await session.execute(
        update(VertexRow)
        .where(VertexRow.id == candidate.id, VertexRow.status == candidate.status)
        .values(status="running", leased_by=agent,
                lease_until=_now() + timedelta(seconds=ttl_seconds),
                attempts=candidate.attempts + 1)
        .returning(VertexRow.id, VertexRow.name, VertexRow.done_when,
                   VertexRow.paths_allow_json, VertexRow.attempts)
    )
    row = result.one()
    await session.commit()
    return {"id": row.id, "name": row.name, "done_when": row.done_when,
            "paths_allow": row.paths_allow_json, "attempts": row.attempts}


async def heartbeat_vertex(session: AsyncSession, vertex_id: int, agent: str,
                           ttl_seconds: int = 300) -> bool:
    result = await session.execute(
        update(VertexRow)
        .where(VertexRow.id == vertex_id, VertexRow.leased_by == agent,
               VertexRow.status == "running")
        .values(lease_until=_now() + timedelta(seconds=ttl_seconds))
    )
    await session.commit()
    return cast(CursorResult[Any], result).rowcount > 0


async def complete_vertex(session: AsyncSession, vertex_id: int, agent: str,
                          result_data: dict[str, Any] | None = None) -> bool:
    result = await session.execute(
        update(VertexRow)
        .where(VertexRow.id == vertex_id, VertexRow.leased_by == agent,
               VertexRow.status == "running")
        .values(status="done", leased_by=None, lease_until=None,
                result_json=result_data or {})
    )
    await session.commit()
    return cast(CursorResult[Any], result).rowcount > 0


async def fail_vertex(session: AsyncSession, vertex_id: int, agent: str) -> bool:
    """Вернуть вершину в очередь (lease потерян / done_when не выполнен)."""
    result = await session.execute(
        update(VertexRow)
        .where(VertexRow.id == vertex_id, VertexRow.leased_by == agent,
               VertexRow.status == "running")
        .values(status="queued", leased_by=None, lease_until=None)
    )
    await session.commit()
    return cast(CursorResult[Any], result).rowcount > 0


async def register_identity(session: AsyncSession, name: str, role: str,
                            allowed_sections: list[str] | None = None) -> int:
    row = IdentityRow(name=name, role=role, allowed_sections_json=allowed_sections or [])
    session.add(row)
    await session.flush()
    return row.id


async def consume_budget(session: AsyncSession, subject: str, tokens: int) -> bool:
    """Списать токены; False если лимит превышен (атомарно)."""
    row = await session.scalar(select(BudgetRow).where(BudgetRow.subject == subject))
    if row is None:
        session.add(BudgetRow(subject=subject, limit_tokens=max(tokens, 0) * 10 or 1000,
                              spent_tokens=0))
        await session.flush()
    result = await session.execute(
        update(BudgetRow)
        .where(BudgetRow.subject == subject,
               BudgetRow.spent_tokens + tokens <= BudgetRow.limit_tokens)
        .values(spent_tokens=BudgetRow.spent_tokens + tokens)
    )
    await session.commit()
    return cast(CursorResult[Any], result).rowcount > 0


async def create_hitl(session: AsyncSession, kind: str, subject: str,
                      context: dict[str, Any] | None = None) -> int:
    row = HITLRow(kind=kind, subject=subject, context_json=context or {})
    session.add(row)
    await session.flush()
    return row.id


async def decide_hitl(session: AsyncSession, request_id: int, action: str,
                      decided_by: str, note: str | None = None) -> str | None:
    """Решение по pending-запросу; None если запрос не в pending."""
    result = await session.execute(
        update(HITLRow)
        .where(HITLRow.id == request_id, HITLRow.status == "pending")
        .values(status=action, decided_by=decided_by, decision_note=note,
                decided_at=_now())
        .returning(HITLRow.status)
    )
    await session.commit()
    row = result.one_or_none()
    return row.status if row else None


async def append_audit(session: AsyncSession, actor: str, action: str, target: str,
                       outcome: str = "ok",
                       payload: dict[str, Any] | None = None) -> int:
    row = AuditRow(actor=actor, action=action, target=target, outcome=outcome,
                   payload_json=payload or {})
    session.add(row)
    await session.flush()
    return row.id


async def enqueue_merge(session: AsyncSession, pr_ref: str, project: str) -> int:
    row = MergeQueueRow(pr_ref=pr_ref, project=project)
    session.add(row)
    await session.flush()
    return row.id
