"""GiteaIssuesClient against httpx.MockTransport (M4c Task 2)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from mcp_servers.gitea_client import GiteaError, GiteaIssuesClient

TOKEN = "gitea-admin-SECRET-0123"
BASE = "http://gitea.test"

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> tuple[GiteaIssuesClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GiteaIssuesClient(http, base_url=BASE, token=TOKEN), http


def _issue(number: int, *, state: str = "open", body: str = "") -> dict[str, object]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": state,
        "body": body,
        "html_url": f"{BASE}/o/r/issues/{number}",
        "pull_request": None,
    }


async def test_list_issues_hits_issues_endpoint_with_token_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[_issue(1), _issue(2)])

    client, http = _client(handler)
    async with http:
        issues = await client.list_issues("o", "r")
    assert [i.number for i in issues] == [1, 2]
    assert issues[0].title == "Issue 1"
    req = seen[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/repos/o/r/issues"
    assert req.url.params["state"] == "open"
    assert req.url.params["type"] == "issues"
    assert req.headers["Authorization"] == f"token {TOKEN}"


async def test_get_issue_returns_detail_with_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/repos/o/r/issues/42"
        return httpx.Response(200, json=_issue(42, body="do the thing"))

    client, http = _client(handler)
    async with http:
        detail = await client.get_issue("o", "r", 42)
    assert detail.number == 42
    assert detail.body == "do the thing"
    assert detail.state == "open"


async def test_set_state_patches_issue_state() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=_issue(7, state="closed"))

    client, http = _client(handler)
    async with http:
        await client.set_state("o", "r", 7, "closed")
    assert seen[0].method == "PATCH"
    assert seen[0].url.path == "/api/v1/repos/o/r/issues/7"
    assert json.loads(seen[0].content) == {"state": "closed"}


async def test_set_state_rejects_unknown_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("no request may be sent for an invalid state")

    client, http = _client(handler)
    async with http:
        with pytest.raises(ValueError):
            await client.set_state("o", "r", 7, "in-progress")


async def test_add_comment_posts_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": 1, "body": "hi"})

    client, http = _client(handler)
    async with http:
        await client.add_comment("o", "r", 7, "hi")
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/v1/repos/o/r/issues/7/comments"
    assert json.loads(seen[0].content) == {"body": "hi"}


async def test_error_is_gitea_error_with_token_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A hostile/buggy upstream echoing the credential back.
        return httpx.Response(404, text=f"not found (auth: token {TOKEN})")

    client, http = _client(handler)
    async with http:
        with pytest.raises(GiteaError) as excinfo:
            await client.get_issue("o", "r", 999)
    message = str(excinfo.value)
    assert "404" in message
    assert TOKEN not in message
    assert "***" in message


async def test_transport_error_is_gitea_error_with_token_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {TOKEN}", request=request)

    client, http = _client(handler)
    async with http:
        with pytest.raises(GiteaError) as excinfo:
            await client.list_issues("o", "r")
    assert TOKEN not in str(excinfo.value)
