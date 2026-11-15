"""Automations in the control plane (M10): definitions as code + runs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from agent_api.main import create_app
from agent_api.ui_auth import hash_password, parse_users
from httpx2 import ASGITransport, AsyncClient

DEFINITION = """name: vuln-scan
description: SAST + secret scan of the selected repos.
trigger: manual
scope: [swarm-lab/vuln-demo]
role: security-specialist
budget_minutes: 20
workflow: vuln-scan
"""


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    defs = tmp_path / "automations"
    defs.mkdir()
    (defs / "vuln-scan.yaml").write_text(DEFINITION, encoding="utf-8")
    (defs / "broken.yaml").write_text("name: [unclosed", encoding="utf-8")
    app = create_app()
    app.state.override_database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    app.state.automations_dir = str(defs)
    app.state.ui_users = parse_users(f"alice:{hash_password('pw')}:admin")
    app.state.session_secret = "s3cret-for-tests"
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


async def test_definitions_listed_and_broken_skipped(client: AsyncClient) -> None:
    data = (await client.get("/automations")).json()
    assert [a["name"] for a in data["automations"]] == ["vuln-scan"]
    assert data["automations"][0]["scope"] == ["swarm-lab/vuln-demo"]
    assert data["invalid"] == ["broken.yaml"]


async def test_run_lifecycle(client: AsyncClient) -> None:
    rid = (await client.post("/automations/runs", json={"name": "vuln-scan"})).json()["id"]
    assert (await client.get("/automations/runs", params={"status": "requested"})).json()["runs"][
        0
    ]["id"] == rid
    patch = {"status": "awaiting_approval", "report": {"findings": [{"rule": "r"}]}, "hitl_id": 4}
    assert (await client.patch(f"/automations/runs/{rid}", json=patch)).status_code == 200
    run = (await client.get("/automations/runs", params={"name": "vuln-scan"})).json()["runs"][0]
    assert run["status"] == "awaiting_approval" and run["hitl_id"] == 4
    assert run["report"]["findings"] == [{"rule": "r"}]
    done = {"status": "done", "created_issues": ["http://g/i/1"]}
    await client.patch(f"/automations/runs/{rid}", json=done)
    assert (await client.get("/automations/runs")).json()["runs"][0]["created_issues"] == [
        "http://g/i/1"
    ]


async def test_bad_status_and_unknown_run(client: AsyncClient) -> None:
    rid = (await client.post("/automations/runs", json={"name": "vuln-scan"})).json()["id"]
    assert (
        await client.patch(f"/automations/runs/{rid}", json={"status": "nope"})
    ).status_code == 422
    assert (await client.patch("/automations/runs/999", json={"status": "done"})).status_code == 404
    assert (await client.post("/automations/runs", json={"name": "ghost"})).status_code == 404


async def test_ui_run_request_needs_login_and_records_user(client: AsyncClient) -> None:
    assert (await client.post("/ui/api/automations/vuln-scan/run")).status_code == 401
    await client.post("/ui/login", data={"username": "alice", "password": "pw"})
    r = await client.post("/ui/api/automations/vuln-scan/run")
    assert r.status_code == 200
    run = (await client.get("/automations/runs")).json()["runs"][0]
    assert run["status"] == "requested" and run["requested_by"] == "alice"
