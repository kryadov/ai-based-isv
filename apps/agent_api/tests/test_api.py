"""Control plane API tests (httpx AsyncClient over the app)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from agent_api.main import create_app
from httpx2 import ASGITransport, AsyncClient


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


async def test_healthz(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200


async def test_full_run_lifecycle(client: AsyncClient) -> None:
    r = await client.post("/runs", json={"project": "isv-product", "task_ref": "TRK-9"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    r = await client.post(f"/runs/{run_id}/vertices", json={"name": "only", "done_when": "green"})
    assert r.status_code == 200

    r = await client.post("/vertices/claim", json={"run_id": run_id, "agent": "w1"})
    claimed = r.json()["vertex"]
    assert claimed is not None and claimed["name"] == "only"

    r = await client.post(f"/vertices/{claimed['id']}/heartbeat", json={"agent": "w1"})
    assert r.status_code == 200

    r = await client.post(
        f"/vertices/{claimed['id']}/complete", json={"agent": "w1", "result": {"pr": 7}}
    )
    assert r.status_code == 200


async def test_claim_conflict_wrong_agent(client: AsyncClient) -> None:
    r = await client.post("/runs", json={"project": "p", "task_ref": "t"})
    run_id = r.json()["run_id"]
    await client.post(f"/runs/{run_id}/vertices", json={"name": "v", "done_when": "d"})
    r = await client.post("/vertices/claim", json={"run_id": run_id, "agent": "w"})
    vid = r.json()["vertex"]["id"]
    r = await client.post(f"/vertices/{vid}/complete", json={"agent": "not-owner"})
    assert r.status_code == 409


async def test_hitl_approve_and_double_decision(client: AsyncClient) -> None:
    r = await client.post("/hitl", json={"kind": "merge", "subject": "PR #1"})
    rid = r.json()["request_id"]
    r = await client.get("/hitl/pending")
    assert r.json()["pending"] and r.json()["pending"][0]["id"] == rid
    r = await client.post(
        f"/hitl/{rid}/decision", json={"action": "approve", "decided_by": "kryadov"}
    )
    assert r.json()["status"] == "approved"
    r = await client.post(f"/hitl/{rid}/decision", json={"action": "reject", "decided_by": "x"})
    assert r.status_code == 409


async def test_budget_exceeded_conflict(client: AsyncClient) -> None:
    r = await client.post("/runs", json={"project": "p", "task_ref": "t"})
    run_id = r.json()["run_id"]
    r = await client.post(f"/budgets/{run_id}", json={"limit_tokens": 100})
    assert r.status_code == 200
    r = await client.post(f"/budgets/{run_id}/consume", json={"tokens": 100})
    assert r.status_code == 200
    r = await client.post(f"/budgets/{run_id}/consume", json={"tokens": 1})
    assert r.status_code == 402


async def test_audit_append_and_list(client: AsyncClient) -> None:
    await client.post(
        "/audit", json={"actor": "w1", "action": "vertex.complete", "target": "vertex:3"}
    )
    r = await client.get("/audit", params={"target": "vertex:3"})
    events = r.json()["events"]
    assert events and events[0]["target"] == "vertex:3"
