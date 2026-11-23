"""Organization panel (M10): human login, app shell, i18n, Inbox decisions."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from agent_api.main import create_app
from agent_api.ui_auth import UI_DIR, hash_password, parse_users, sign, verify
from httpx2 import ASGITransport, AsyncClient

SECRET = "test-secret-0123456789"
USERS = f"alice:{hash_password('wonderland')}:admin"
TABS = ("inbox", "projects", "swarm", "pipeline", "automations", "metrics", "memory", "settings")


def _app(tmp_path: Path, users: str = USERS) -> object:
    app = create_app()
    app.state.override_database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    app.state.ui_users = parse_users(users)
    app.state.session_secret = SECRET
    return app


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    app = _app(tmp_path)
    async with (
        app.router.lifespan_context(app),  # type: ignore[attr-defined]
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,  # type: ignore[arg-type]
    ):
        yield c


async def _login(client: AsyncClient) -> None:
    r = await client.post("/ui/login", data={"username": "alice", "password": "wonderland"})
    assert r.status_code in (200, 303), r.text
    assert "swarm_session" in client.cookies


def test_signed_session_round_trip_and_tamper() -> None:
    token = sign("alice", SECRET, ttl_s=60)
    assert verify(token, SECRET) == "alice"
    assert verify(token + "x", SECRET) is None
    assert verify(token, "other-secret") is None
    assert verify(sign("alice", SECRET, ttl_s=-1, now=time.time()), SECRET) is None


def test_parse_users_skips_garbage() -> None:
    users = parse_users(f"{USERS}, broken ,bob:{hash_password('x')}:viewer")
    assert set(users) == {"alice", "bob"} and users["bob"].role == "viewer"


async def test_unauthenticated_gets_login_page(client: AsyncClient) -> None:
    r = await client.get("/ui")
    assert r.status_code == 200 and 'id="login-form"' in r.text
    assert (await client.get("/ui/api/me")).status_code == 401


async def test_bad_password_refused(client: AsyncClient) -> None:
    r = await client.post("/ui/login", data={"username": "alice", "password": "nope"})
    assert r.status_code == 401 and "swarm_session" not in client.cookies


async def test_login_shows_shell_with_all_tabs(client: AsyncClient) -> None:
    await _login(client)
    shell = (await client.get("/ui")).text
    for tab in TABS:
        assert f'data-tab="{tab}"' in shell, tab
    me = (await client.get("/ui/api/me")).json()
    assert me == {"user": "alice", "role": "admin"}
    assert (await client.get("/ui/static/app.js")).status_code == 200


async def test_panel_disabled_without_users(tmp_path: Path) -> None:
    app = _app(tmp_path, users="")
    async with (
        app.router.lifespan_context(app),  # type: ignore[attr-defined]
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,  # type: ignore[arg-type]
    ):
        r = await c.get("/ui")
        assert r.status_code == 503 and "AGENT_API_UI_USERS" in r.text


def test_i18n_key_sets_match() -> None:
    en = json.loads((UI_DIR / "i18n" / "en.json").read_text(encoding="utf-8"))
    ru = json.loads((UI_DIR / "i18n" / "ru.json").read_text(encoding="utf-8"))
    assert set(en) == set(ru) and len(en) >= 20
    for tab in TABS:
        assert f"tab.{tab}" in en


async def test_inbox_decision_records_user_and_refuses_twice(client: AsyncClient) -> None:
    hid = (
        await client.post("/hitl", json={"kind": "merge", "subject": "pr 7", "context": {}})
    ).json()["request_id"]
    assert (
        await client.post(f"/ui/api/hitl/{hid}/decision", json={"action": "approve"})
    ).status_code == 401
    await _login(client)
    r = await client.post(f"/ui/api/hitl/{hid}/decision", json={"action": "approve", "note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    detail = (await client.get(f"/hitl/{hid}")).json()
    assert detail["status"] == "approved"
    again = await client.post(f"/ui/api/hitl/{hid}/decision", json={"action": "reject"})
    assert again.status_code == 409
    audit = (await client.get("/audit", params={"target": f"hitl:{hid}"})).json()["events"]
    assert any(e["actor"] == "ui:alice" for e in audit)


def test_every_tab_says_something_when_it_has_no_data() -> None:
    """M12 (found by the CI browser e2e): an empty Memory tab rendered only a
    search box — every list view names its empty state."""
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    en = json.loads((UI_DIR / "i18n" / "en.json").read_text(encoding="utf-8"))
    for key in ("inbox.empty", "swarm.empty", "memory.empty"):
        assert f't("{key}")' in js, key
        assert key in en, key
