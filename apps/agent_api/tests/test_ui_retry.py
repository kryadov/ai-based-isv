"""Panel retry (M12): a human starts a new run for a task whose newest run failed;
the dispatcher plans the new (vertex-less, pending) run on its next tick."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from agent_api.main import create_app
from agent_api.ui_auth import hash_password, parse_users
from httpx2 import ASGITransport, AsyncClient

SECRET = "test-secret-0123456789"
USERS = f"alice:{hash_password('wonderland')}:admin,bob:{hash_password('builder')}:viewer"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.state.override_database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    app.state.ui_users = parse_users(USERS)
    app.state.session_secret = SECRET
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


async def _login(client: AsyncClient, user: str = "alice", pw: str = "wonderland") -> None:
    r = await client.post("/ui/login", data={"username": user, "password": pw})
    assert r.status_code in (200, 303), r.text


async def _run(client: AsyncClient, status: str, task_ref: str = "swarm-lab/demo#7") -> int:
    created = await client.post(
        "/runs", json={"project": "intake:swarm-lab/demo", "task_ref": task_ref}
    )
    run_id = int(created.json()["run_id"])
    if status != "pending":
        await client.post(f"/runs/{run_id}/status", json={"status": status, "actor": "t"})
    return run_id


async def test_retry_of_a_failed_run_creates_a_new_pending_run(client: AsyncClient) -> None:
    failed = await _run(client, "failed")
    await _login(client)
    r = await client.post(f"/ui/api/runs/{failed}/retry")
    assert r.status_code == 200, r.text
    new_id = r.json()["run_id"]
    runs = (await client.get("/runs", params={"task_ref": "swarm-lab/demo#7"})).json()["runs"]
    assert runs[0]["id"] == new_id and runs[0]["status"] == "pending"
    assert runs[0]["project"] == "intake:swarm-lab/demo" and runs[0]["vertex_count"] == 0
    audit = (await client.get("/audit", params={"target": f"run:{failed}"})).json()
    events = audit["events"] if isinstance(audit, dict) else audit
    assert any(e["action"] == "run_retry" and e["actor"] == "ui:alice" for e in events)


@pytest.mark.parametrize("status", ["done", "pending"])
async def test_retry_refused_unless_failed(client: AsyncClient, status: str) -> None:
    run_id = await _run(client, status)
    await _login(client)
    assert (await client.post(f"/ui/api/runs/{run_id}/retry")).status_code == 409


async def test_retry_only_of_the_newest_run_of_the_task(client: AsyncClient) -> None:
    old = await _run(client, "failed")
    await _run(client, "pending")
    await _login(client)
    assert (await client.post(f"/ui/api/runs/{old}/retry")).status_code == 409


async def test_retry_needs_admin_and_login(client: AsyncClient) -> None:
    run_id = await _run(client, "failed")
    assert (await client.post(f"/ui/api/runs/{run_id}/retry")).status_code == 401
    await _login(client, "bob", "builder")
    assert (await client.post(f"/ui/api/runs/{run_id}/retry")).status_code == 403
    assert (await client.post("/ui/api/runs/999/retry")).status_code in (403, 404)
