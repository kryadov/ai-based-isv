"""Project episodic memory: store, ranking, API (M7)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from agent_api.main import create_app
from agent_api.store import rank_episodes
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_api import store

NOW = datetime(2026, 11, 16, tzinfo=UTC)


def _ep(i: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": i,
        "project": "p",
        "kind": "lesson",
        "title": "",
        "body": "",
        "paths": [],
        "tags": [],
        "created_at": (NOW - timedelta(days=30)).isoformat(),
    }
    base.update(kw)
    return base


def test_path_match_scores_three() -> None:
    ranked = rank_episodes(
        [_ep(1, paths=["src/isv_product/main.py"]), _ep(2, paths=["docs/x.md"])],
        paths=["src/**"],
        text="",
        now=NOW,
        limit=5,
    )
    assert [e["id"] for e in ranked] == [1]
    assert ranked[0]["score"] == 3


def test_shared_prefix_counts_as_path_match() -> None:
    ranked = rank_episodes(
        [_ep(1, paths=["src/isv_product/main.py"])],
        paths=["src/isv_product/other.py"],
        text="",
        now=NOW,
        limit=5,
    )
    assert ranked and ranked[0]["score"] == 3


def test_words_capped_kind_and_recency_boosts() -> None:
    words = "canary rollback latency errors timeout greeting endpoint metrics"
    ranked = rank_episodes(
        [
            _ep(1, title=words, body=words),
            _ep(2, kind="postmortem", title="canary rollback", created_at=NOW.isoformat()),
        ],
        paths=[],
        text=words,
        now=NOW,
        limit=5,
    )
    by_id = {e["id"]: e["score"] for e in ranked}
    assert by_id[1] == 5  # word overlap capped at +5
    assert by_id[2] == 2 + 2 + 1  # two words + postmortem + recent


def test_stop_words_and_short_words_ignored() -> None:
    ranked = rank_episodes(
        [_ep(1, title="the and with this that from")],
        paths=[],
        text="the and with",
        now=NOW,
        limit=5,
    )
    assert ranked == []


def test_empty_query_returns_nothing() -> None:
    assert (
        rank_episodes([_ep(1, paths=["a"], title="x")], paths=[], text="", now=NOW, limit=5) == []
    )


def test_ties_prefer_newer_and_limit_applies() -> None:
    eps = [
        _ep(i, paths=["src/a.py"], created_at=(NOW - timedelta(days=30 + i)).isoformat())
        for i in range(1, 6)
    ]
    ranked = rank_episodes(eps, paths=["src/**"], text="", now=NOW, limit=2)
    assert [e["id"] for e in ranked] == [1, 2]


async def test_store_trims_long_fields(session: AsyncSession) -> None:
    eid = await store.add_episode(
        session, project="p", kind="postmortem", title="t" * 500, body="b" * 9000, paths=[], tags=[]
    )
    ep = await store.get_episode(session, eid)
    assert ep is not None
    assert len(ep["title"]) == 200 and len(ep["body"]) == 8000


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.state.override_database_url = f"sqlite+aiosqlite:///{tmp_path / 'api.db'}"
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


async def test_api_round_trip(client: AsyncClient) -> None:
    r = await client.post(
        "/memory/episodes",
        json={
            "project": "swarm-lab/isv-product",
            "kind": "postmortem",
            "title": "canary rolled back: error-ratio",
            "body": "5xx ratio 0.5 vs bound 0.05",
            "paths": ["src/isv_product/main.py"],
            "run_id": 3,
        },
    )
    assert r.status_code == 200
    eid = r.json()["id"]
    got = (await client.get(f"/memory/episodes/{eid}")).json()
    assert got["kind"] == "postmortem" and got["run_id"] == 3
    sl = await client.get(
        "/memory/slice",
        params={"project": "swarm-lab/isv-product", "paths": "src/**", "q": "add endpoint"},
    )
    assert [e["id"] for e in sl.json()["episodes"]] == [eid]
    other = await client.get("/memory/slice", params={"project": "x/y", "paths": "src/**"})
    assert other.json()["episodes"] == []


async def test_api_rejects_unknown_kind(client: AsyncClient) -> None:
    r = await client.post(
        "/memory/episodes", json={"project": "p", "kind": "gossip", "title": "t", "body": "b"}
    )
    assert r.status_code == 422


async def test_api_limit_is_capped(client: AsyncClient) -> None:
    for i in range(25):
        await client.post(
            "/memory/episodes",
            json={
                "project": "p",
                "kind": "lesson",
                "title": f"t{i}",
                "body": "b",
                "paths": ["a/b"],
            },
        )
    sl = await client.get("/memory/slice", params={"project": "p", "paths": "a/**", "limit": 100})
    assert len(sl.json()["episodes"]) == 20


async def test_unknown_episode_404(client: AsyncClient) -> None:
    assert (await client.get("/memory/episodes/999")).status_code == 404
