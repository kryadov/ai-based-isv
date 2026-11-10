"""Control plane integration test against live Postgres (CNPG on the stand).

Runs only if M3_INTEGRATION_PG_URL is set (opt-in), e.g.:
  postgresql+asyncpg://gitea:<pw>@localhost:15432/swarm
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from agent_api.main import create_app
from httpx2 import ASGITransport, AsyncClient

PG_URL = os.environ.get("M3_INTEGRATION_PG_URL", "")

pytestmark = pytest.mark.skipif(not PG_URL, reason="M3_INTEGRATION_PG_URL not set")


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    import contextlib

    app = create_app()
    app.state.override_database_url = PG_URL  # read in lifespan
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c
    with contextlib.suppress(Exception):
        pass


async def test_full_lifecycle_on_postgres(client: AsyncClient) -> None:
    r = await client.post("/runs", json={"project": "m3-integration", "task_ref": "INTEG-1"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    r = await client.post(f"/runs/{run_id}/vertices", json={"name": "integ", "done_when": "done"})
    assert r.status_code == 200
    r = await client.post("/vertices/claim", json={"run_id": run_id, "agent": "w"})
    assert r.status_code == 200 and r.json()["vertex"]
    r = await client.post(
        "/audit", json={"actor": "w", "action": "integ.done", "target": f"run:{run_id}"}
    )
    assert r.status_code == 200
    r = await client.get("/audit", params={"target": f"run:{run_id}"})
    assert r.json()["events"]
