"""Lease release (M12): a worker that is shut down gives its vertex back — the
vertex is queued again at once and the attempt does not count."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from agent_api.main import create_app
from agent_api.models import VertexRow
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_api import store


async def _claimed(session: AsyncSession, agent: str = "w1") -> int:
    run_id = await store.create_run(session, project="p", task_ref="o/r#1")
    await store.add_vertex(session, run_id, name="v", done_when="ok")
    claimed = await store.claim_vertex(session, run_id, agent=agent)
    assert claimed is not None and claimed["attempts"] == 1
    return int(claimed["id"])


async def _row(session: AsyncSession, vertex_id: int) -> VertexRow:
    session.expire_all()
    row = (await session.scalars(select(VertexRow).where(VertexRow.id == vertex_id))).one()
    return row


async def test_release_requeues_and_gives_the_attempt_back(session: AsyncSession) -> None:
    vertex_id = await _claimed(session)
    assert await store.release_vertex(session, vertex_id, agent="w1") is True
    row = await _row(session, vertex_id)
    assert (row.status, row.leased_by, row.lease_until, row.attempts) == ("queued", None, None, 0)


async def test_release_by_another_agent_is_refused(session: AsyncSession) -> None:
    vertex_id = await _claimed(session)
    assert await store.release_vertex(session, vertex_id, agent="intruder") is False
    row = await _row(session, vertex_id)
    assert (row.status, row.leased_by, row.attempts) == ("running", "w1", 1)


async def test_release_never_drives_attempts_negative(session: AsyncSession) -> None:
    vertex_id = await _claimed(session)
    row = await _row(session, vertex_id)
    row.attempts = 0
    await session.commit()
    assert await store.release_vertex(session, vertex_id, agent="w1") is True
    assert (await _row(session, vertex_id)).attempts == 0


async def test_released_vertex_is_claimable_again(session: AsyncSession) -> None:
    vertex_id = await _claimed(session)
    await store.release_vertex(session, vertex_id, agent="w1")
    run_id = (await _row(session, vertex_id)).run_id
    again = await store.claim_vertex(session, run_id, agent="w2")
    assert again is not None and again["id"] == vertex_id and again["attempts"] == 1


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.state.override_database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


async def test_release_endpoint(client: AsyncClient) -> None:
    run_id = (await client.post("/runs", json={"project": "p", "task_ref": "o/r#1"})).json()[
        "run_id"
    ]
    await client.post(f"/runs/{run_id}/vertices", json={"name": "v", "done_when": "ok"})
    vertex = (await client.post("/vertices/claim", json={"run_id": run_id, "agent": "w1"})).json()[
        "vertex"
    ]
    refused = await client.post(f"/vertices/{vertex['id']}/release", json={"agent": "w2"})
    assert refused.status_code == 409
    ok = await client.post(f"/vertices/{vertex['id']}/release", json={"agent": "w1"})
    assert ok.status_code == 200 and ok.json() == {"ok": True, "requeued": True}
