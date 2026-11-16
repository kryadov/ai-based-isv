"""Service-API token (M11): the in-cluster control plane is reachable from the
LAN on the stand (NodePorts bind 0.0.0.0), so every service endpoint needs the
bearer token; the panel keeps its own login; health and metrics stay open."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from agent_api.main import create_app
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

TOKEN = "s3rvice-token"


def _client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str
) -> tuple[FastAPI, AsyncClient]:
    monkeypatch.setenv("AGENT_API_SERVICE_TOKEN", token)
    app = create_app()
    app.state.override_database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def guarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    app, client = _client(tmp_path, monkeypatch, TOKEN)
    async with app.router.lifespan_context(app), client:
        yield client


async def test_service_endpoint_needs_the_token(guarded: AsyncClient) -> None:
    assert (await guarded.get("/runs")).status_code == 401
    bad = {"Authorization": "Bearer wrong"}
    assert (await guarded.get("/runs", headers=bad)).status_code == 401
    assert (await guarded.post("/hitl/1/decision", json={})).status_code == 401
    ok = {"Authorization": f"Bearer {TOKEN}"}
    assert (await guarded.get("/runs", headers=ok)).status_code == 200


async def test_open_paths_stay_open(guarded: AsyncClient) -> None:
    assert (await guarded.get("/healthz")).status_code == 200
    assert (await guarded.get("/metrics")).status_code == 200
    assert (await guarded.get("/ui")).status_code in (200, 503)  # login page / panel disabled
    assert (await guarded.get("/ui/api/me")).status_code in (401, 503)  # session, not token


async def test_without_a_configured_token_the_api_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = _client(tmp_path, monkeypatch, "")
    async with app.router.lifespan_context(app), client:
        assert (await client.get("/runs")).status_code == 200


async def test_panel_session_may_read_but_not_act(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_api.ui_auth import hash_password

    monkeypatch.setenv("AGENT_API_UI_USERS", f"op:{hash_password('pw')}:admin")
    monkeypatch.setenv("AGENT_API_SESSION_SECRET", "sess")
    app, client = _client(tmp_path, monkeypatch, TOKEN)
    async with app.router.lifespan_context(app), client:
        await client.post("/ui/login", data={"username": "op", "password": "pw"})
        assert client.cookies.get("swarm_session")
        assert (await client.get("/automations")).status_code == 200
        assert (await client.get("/automations/runs")).status_code == 200
        assert (await client.post("/automations/runs", json={"name": "x"})).status_code == 401
        assert (await client.post("/hitl/1/decision", json={})).status_code == 401
        client.cookies.clear()
        client.cookies.set("swarm_session", "forged")
        assert (await client.get("/automations")).status_code == 401
