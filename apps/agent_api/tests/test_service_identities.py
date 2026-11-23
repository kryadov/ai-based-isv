"""Per-identity service tokens (M12): the daemon, workers, the automation poller
and the operator each hold their own bearer token; the control plane keeps only
their sha256 hashes, knows who is calling, and lets only the operator decide a
HITL request (a worker can never approve its own merge)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from agent_api.main import create_app, parse_service_tokens
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

TOKENS = {"daemon": "tok-d", "worker": "tok-w", "poller": "tok-p", "operator": "tok-o"}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


SPEC = ",".join(f"{name}:{_sha(tok)}" for name, tok in TOKENS.items())


def _bearer(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[name]}"}


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec: str, legacy: str = "") -> FastAPI:
    monkeypatch.setenv("AGENT_API_SERVICE_TOKENS", spec)
    monkeypatch.setenv("AGENT_API_SERVICE_TOKEN", legacy)
    app = create_app()
    app.state.override_database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    return app


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    app = _app(tmp_path, monkeypatch, SPEC)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


def test_parse_service_tokens_maps_hashes_to_names() -> None:
    parsed = parse_service_tokens(f" daemon:{_sha('a')} , bad-entry, worker:{_sha('b')},:x,")
    assert parsed == {_sha("a"): "daemon", _sha("b"): "worker"}
    assert parse_service_tokens("") == {}


async def test_every_identity_token_is_accepted(client: AsyncClient) -> None:
    for name in TOKENS:
        assert (await client.get("/runs", headers=_bearer(name))).status_code == 200, name
    assert (await client.get("/runs")).status_code == 401
    assert (await client.get("/runs", headers={"Authorization": "Bearer "})).status_code == 401
    # a hash is not a token
    leaked = {"Authorization": f"Bearer {_sha(TOKENS['worker'])}"}
    assert (await client.get("/runs", headers=leaked)).status_code == 401


async def _pending_hitl(client: AsyncClient) -> int:
    created = await client.post(
        "/hitl",
        json={"kind": "merge", "subject": "pr#1", "payload": {}},
        headers=_bearer("worker"),
    )
    assert created.status_code == 200, created.text
    return int(created.json()["request_id"])


async def test_only_the_operator_decides_hitl(client: AsyncClient) -> None:
    request_id = await _pending_hitl(client)
    for name in ("worker", "daemon", "poller"):
        refused = await client.post(
            f"/hitl/{request_id}/decision",
            json={"action": "approve", "decided_by": name},
            headers=_bearer(name),
        )
        assert refused.status_code == 403, name
    ok = await client.post(
        f"/hitl/{request_id}/decision",
        json={"action": "approve", "decided_by": "op"},
        headers=_bearer("operator"),
    )
    assert ok.status_code == 200


async def test_audit_rows_record_the_calling_identity(client: AsyncClient) -> None:
    await client.post(
        "/audit",
        json={"actor": "swarm-w1", "action": "claim", "target": "vertex:1", "payload": {"a": 1}},
        headers=_bearer("worker"),
    )
    rows = (await client.get("/audit", headers=_bearer("operator"))).json()
    events = rows["events"] if isinstance(rows, dict) else rows
    row = next(e for e in events if e["action"] == "claim")
    assert row["payload"]["identity"] == "worker"
    assert row["payload"]["a"] == 1


async def test_legacy_single_token_is_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path, monkeypatch, f"worker:{_sha('tok-w')}", legacy="legacy-tok")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        request_id = await _pending_hitl(c)
        decided = await c.post(
            f"/hitl/{request_id}/decision",
            json={"action": "approve"},
            headers={"Authorization": "Bearer legacy-tok"},
        )
        assert decided.status_code == 200


async def test_open_mode_still_decides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No token configured at all (host-run flows on 127.0.0.1): unchanged, open."""
    app = _app(tmp_path, monkeypatch, "")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        created = await c.post("/hitl", json={"kind": "merge", "subject": "pr#2", "payload": {}})
        request_id = int(created.json()["request_id"])
        assert (await c.post(f"/hitl/{request_id}/decision", json={})).status_code == 200
