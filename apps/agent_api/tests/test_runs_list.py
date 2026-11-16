"""Run lookup for the dispatcher (M11): dedup by task_ref, filters, vertex count."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from agent_api.main import create_app
from httpx2 import ASGITransport, AsyncClient


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.state.override_database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


async def _run(client: AsyncClient, ref: str) -> int:
    body = {"project": ref.split("#")[0], "task_ref": ref}
    return int((await client.post("/runs", json=body)).json()["run_id"])


async def test_runs_filter_by_task_ref(client: AsyncClient) -> None:
    await _run(client, "o/r#1")
    second = await _run(client, "o/r#2")
    runs = (await client.get("/runs", params={"task_ref": "o/r#2"})).json()["runs"]
    assert [r["id"] for r in runs] == [second]
    assert runs[0]["project"] == "o/r"
    assert runs[0]["status"] == "pending"


async def test_runs_filter_by_status(client: AsyncClient) -> None:
    first = await _run(client, "o/r#1")
    second = await _run(client, "o/r#2")
    await client.post(f"/runs/{first}/status", json={"status": "done", "actor": "judge"})
    pending = (await client.get("/runs", params={"status": "pending"})).json()["runs"]
    assert [r["id"] for r in pending] == [second]
    everything = (await client.get("/runs")).json()["runs"]
    assert [r["id"] for r in everything] == [second, first]  # newest first


async def test_runs_vertex_count(client: AsyncClient) -> None:
    run_id = await _run(client, "o/r#1")
    for name in ("a", "b"):
        await client.post(f"/runs/{run_id}/vertices", json={"name": name, "done_when": "x"})
    runs = (await client.get("/runs", params={"task_ref": "o/r#1"})).json()["runs"]
    assert runs[0]["vertex_count"] == 2
