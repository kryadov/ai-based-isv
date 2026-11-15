"""Gitea PR / commit-status / review client (M5 Task 1) over a fake Gitea."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from agent_runtime.gitea_prs import GiteaPRClient, GiteaPRError, ReviewComment

TOKEN = "gitea-admin-SECRET-0123"
BASE = "http://gitea.test"


class FakeGitea:
    def __init__(self) -> None:
        self.prs: list[dict[str, Any]] = []
        self.statuses: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, text=f"boom token {TOKEN}")
        path = request.url.path.removeprefix("/api/v1/repos/o/r")
        if path == "/pulls" and request.method == "GET":
            state = request.url.params.get("state", "open")
            return httpx.Response(200, json=[p for p in self.prs if p["state"] == state])
        if path == "/pulls" and request.method == "POST":
            body = json.loads(request.content)
            pr = {
                "number": 3,
                "html_url": f"{BASE}/o/r/pulls/3",
                "state": "open",
                "merged": False,
                "head": {"ref": body["head"], "sha": "abc123"},
            }
            self.prs.append(pr)
            return httpx.Response(201, json=pr)
        if path.startswith("/commits/") and path.endswith("/status"):
            sha = path.split("/")[2]
            return httpx.Response(200, json=self.statuses.get(sha, {"state": "", "statuses": []}))
        if path == "/pulls/3/merge" and request.method == "POST":
            return httpx.Response(200)
        return httpx.Response(404, text="not found")


@pytest.fixture
def gitea() -> FakeGitea:
    return FakeGitea()


@pytest.fixture
def client(gitea: FakeGitea) -> GiteaPRClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(gitea.handler))
    return GiteaPRClient(http, base_url=BASE, token=TOKEN)


async def test_ensure_pr_creates_when_none_open(client: GiteaPRClient, gitea: FakeGitea) -> None:
    pr = await client.ensure_pr("o", "r", head="agent/vertex-5", base="main", title="t", body="b")
    assert pr.number == 3
    assert pr.head_sha == "abc123"
    assert [r.method for r in gitea.requests].count("POST") == 1


async def test_ensure_pr_reuses_open_pr_for_head(client: GiteaPRClient, gitea: FakeGitea) -> None:
    gitea.prs.append(
        {
            "number": 7,
            "html_url": f"{BASE}/o/r/pulls/7",
            "state": "open",
            "merged": False,
            "head": {"ref": "agent/vertex-5", "sha": "def456"},
        }
    )
    pr = await client.ensure_pr("o", "r", head="agent/vertex-5", base="main", title="t", body="b")
    assert pr.number == 7
    assert all(r.method == "GET" for r in gitea.requests)


@pytest.mark.parametrize("state", ["success", "failure", "error", "pending"])
async def test_combined_status_maps_states(
    client: GiteaPRClient, gitea: FakeGitea, state: str
) -> None:
    gitea.statuses["abc"] = {"state": state, "statuses": [{"context": "ci", "status": state}]}
    assert await client.combined_status("o", "r", "abc") == state


async def test_combined_status_without_statuses_is_pending(client: GiteaPRClient) -> None:
    assert await client.combined_status("o", "r", "nothing-yet") == "pending"


async def test_merge_sends_merge_style(client: GiteaPRClient, gitea: FakeGitea) -> None:
    await client.merge("o", "r", 3, message="m")
    body = json.loads(gitea.requests[-1].content)
    assert body["Do"] == "merge"


async def test_errors_never_leak_token(client: GiteaPRClient, gitea: FakeGitea) -> None:
    gitea.fail_with = 500
    with pytest.raises(GiteaPRError) as excinfo:
        await client.post_review(
            "o",
            "r",
            3,
            body="x",
            comments=[ReviewComment(path="a", line=1, body="b")],
            event="COMMENT",
        )
    assert TOKEN not in str(excinfo.value)
