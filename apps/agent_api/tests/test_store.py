"""Тесты store: lease-очередь, идемпотентность, HITL, бюджет, audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent_api import store


async def _make_run_with_vertices(session: AsyncSession, count: int = 1,
                                  deps_of_second: list[str] | None = None) -> int:
    run_id = await store.create_run(session, project="isv-product", task_ref="TRK-1")
    await store.add_vertex(session, run_id, name=f"v1-{count}", done_when="t green")
    if count > 1:
        await store.add_vertex(session, run_id, name=f"v2-{count}", done_when="ok",
                               deps=deps_of_second or [f"v1-{count}"])
    return run_id


async def test_claim_returns_queued_vertex(session: AsyncSession) -> None:
    run_id = await _make_run_with_vertices(session)
    claimed = await store.claim_vertex(session, run_id, agent="worker-1")
    assert claimed is not None
    assert claimed["name"] == f"v1-{run_id}" and claimed["attempts"] == 1


async def test_double_claim_same_agent_does_not_take_running(session: AsyncSession) -> None:
    run_id = await _make_run_with_vertices(session)
    first = await store.claim_vertex(session, run_id, agent="worker-1")
    assert first is not None
    second = await store.claim_vertex(session, run_id, agent="worker-1")
    assert second is None, "running-вершина не должна выдаваться повторно"


async def test_fail_vertex_requeues(session: AsyncSession) -> None:
    run_id = await _make_run_with_vertices(session)
    claimed = await store.claim_vertex(session, run_id, agent="worker-1")
    assert claimed is not None
    assert await store.fail_vertex(session, claimed["id"], agent="worker-1")
    reclaimed = await store.claim_vertex(session, run_id, agent="worker-2")
    assert reclaimed is not None and reclaimed["attempts"] == 2


async def test_complete_vertex_terminal(session: AsyncSession) -> None:
    run_id = await _make_run_with_vertices(session)
    claimed = await store.claim_vertex(session, run_id, agent="w")
    assert claimed is not None
    assert await store.complete_vertex(session, claimed["id"], agent="w",
                                       result_data={"pr": 1})
    again = await store.claim_vertex(session, run_id, agent="w")
    assert again is None, "done-вершина не выдаётся"


async def test_heartbeat_only_by_lease_owner(session: AsyncSession) -> None:
    run_id = await _make_run_with_vertices(session)
    claimed = await store.claim_vertex(session, run_id, agent="w1")
    assert claimed is not None
    assert await store.heartbeat_vertex(session, claimed["id"], agent="w1")
    assert not await store.heartbeat_vertex(session, claimed["id"], agent="intruder")


async def test_expired_lease_is_reclaimable(session: AsyncSession) -> None:
    run_id = await _make_run_with_vertices(session)
    claimed = await store.claim_vertex(session, run_id, agent="w1", ttl_seconds=-1)
    assert claimed is not None
    # lease_until в прошлом → другой воркер может забрать
    reclaimed = await store.claim_vertex(session, run_id, agent="w2")
    assert reclaimed is not None


async def test_deps_block_claim_until_done(session: AsyncSession) -> None:
    session2 = session
    run_id = await store.create_run(session2, "p", "t")
    await store.add_vertex(session2, run_id, name="first", done_when="x")
    await store.add_vertex(session2, run_id, name="second", done_when="y",
                           deps=["first"])
    claimed = await store.claim_vertex(session2, run_id, agent="w")
    assert claimed is not None and claimed["name"] == "first"
    await store.complete_vertex(session2, claimed["id"], agent="w")
    claimed2 = await store.claim_vertex(session2, run_id, agent="w")
    assert claimed2 is not None and claimed2["name"] == "second"


async def test_budget_consume_and_limit(session: AsyncSession) -> None:
    await store.set_budget(session, "run-1", limit_tokens=100)
    assert await store.consume_budget(session, "run-1", 60)
    assert await store.consume_budget(session, "run-1", 30)
    assert not await store.consume_budget(session, "run-1", 100), "превышение лимита"
    assert await store.consume_budget(session, "run-1", 10)


async def test_hitl_decide_once(session: AsyncSession) -> None:
    rid = await store.create_hitl(session, "merge", "PR #42")
    status = await store.decide_hitl(session, rid, "approve", "kryadov")
    assert status == "approved"
    second = await store.decide_hitl(session, rid, "reject", "intruder")
    assert second is None, "повторное решение по закрытому запросу недопустимо"


async def test_audit_append_and_list(session: AsyncSession) -> None:
    await store.append_audit(session, "worker-1", "vertex.complete", "vertex:12",
                             payload={"pr": 1})
    await store.append_audit(session, "human", "hitl.decide", "hitl:3",
                             outcome="rejected")
    events = await session.scalars(
        __import__("sqlalchemy").select(store.AuditRow).order_by(store.AuditRow.id)
    )
    all_events = list(events)
    assert len(all_events) == 2
    assert all_events[0].actor == "worker-1"
    assert all_events[1].outcome == "rejected"
