"""Swarm support in the control plane: run vertices, run verdict, metrics (M8)."""

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


async def _graph(client: AsyncClient) -> int:
    run_id = (await client.post("/runs", json={"project": "p", "task_ref": "o/r#1"})).json()[
        "run_id"
    ]
    for name, deps in (("a", []), ("b", []), ("doc", ["a", "b"])):
        await client.post(
            f"/runs/{run_id}/vertices",
            json={"name": name, "deps": deps, "paths_allow": [f"{name}/**"], "done_when": "x"},
        )
    return int(run_id)


async def test_run_vertices_lists_graph_and_results(client: AsyncClient) -> None:
    run_id = await _graph(client)
    claimed = (await client.post("/vertices/claim", json={"run_id": run_id, "agent": "w1"})).json()
    vid = claimed["vertex"]["id"]
    await client.post(
        f"/vertices/{vid}/complete",
        json={"agent": "w1", "result": {"diff_files": ["a/x.py"], "pr_url": "http://g/pulls/1"}},
    )
    data = (await client.get(f"/runs/{run_id}/vertices")).json()["vertices"]
    by_name = {v["name"]: v for v in data}
    assert set(by_name) == {"a", "b", "doc"}
    assert by_name["doc"]["deps"] == ["a", "b"] and by_name["doc"]["status"] == "queued"
    done = by_name[claimed["vertex"]["name"]]
    assert done["status"] == "done" and done["result"]["pr_url"] == "http://g/pulls/1"


async def test_dependent_is_not_claimable_before_deps(client: AsyncClient) -> None:
    run_id = await _graph(client)
    names = []
    for agent in ("w1", "w2", "w3"):
        got = (await client.post("/vertices/claim", json={"run_id": run_id, "agent": agent})).json()
        names.append(got["vertex"]["name"] if got.get("vertex") else None)
    assert sorted(n for n in names if n) == ["a", "b"] and None in names


async def test_run_status_verdict(client: AsyncClient) -> None:
    run_id = await _graph(client)
    ok = await client.post(f"/runs/{run_id}/status", json={"status": "done", "note": "judge"})
    assert ok.status_code == 200
    bad = await client.post(f"/runs/{run_id}/status", json={"status": "great"})
    assert bad.status_code == 422
    audit = (await client.get("/audit", params={"target": f"run:{run_id}"})).json()
    assert any(e["action"] == "run_status" for e in audit["events"])


async def test_metrics_exposition(client: AsyncClient) -> None:
    run_id = await _graph(client)
    await client.post("/hitl", json={"kind": "merge", "subject": "pr", "context": {}})
    text = (await client.get("/metrics")).text
    assert 'swarm_vertices{status="queued"} 3' in text
    assert 'swarm_hitl_pending{kind="merge"} 1' in text
    assert 'swarm_runs{status="pending"} 1' in text
    assert f"run_id={run_id}" not in text  # aggregate gauges only
