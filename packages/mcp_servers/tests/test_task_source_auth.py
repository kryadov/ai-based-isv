"""task-source bearer token (M12): the write-capable tracker server on the host
NodePort refuses calls without ``Authorization: Bearer <MCP_AUTH_TOKEN>``."""

from __future__ import annotations

import httpx
import pytest
from mcp_servers.task_source import BearerAuthMiddleware, auth_middleware
from starlette.types import Receive, Scope, Send

TOKEN = "s3cret-token"


async def _inner(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"reached"})


def _client(token: str) -> httpx.AsyncClient:
    app = BearerAuthMiddleware(_inner, token=token)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer "},
        {"Authorization": f"token {TOKEN}"},
        {"Authorization": TOKEN},
    ],
)
async def test_calls_without_the_right_bearer_are_refused(headers: dict[str, str]) -> None:
    async with _client(TOKEN) as client:
        response = await client.post("/mcp", headers=headers, json={})
    assert response.status_code == 401
    assert b"reached" not in response.content
    assert TOKEN not in response.text


async def test_the_right_bearer_reaches_the_server() -> None:
    async with _client(TOKEN) as client:
        response = await client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.content == b"reached"


def test_no_token_configured_means_no_middleware() -> None:
    assert auth_middleware("") == []
    assert auth_middleware("   ") == []
    assert len(auth_middleware(TOKEN)) == 1


def test_an_empty_token_cannot_build_the_middleware() -> None:
    with pytest.raises(ValueError):
        BearerAuthMiddleware(_inner, token="")
