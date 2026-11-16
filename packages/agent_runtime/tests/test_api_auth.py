"""Service-API token on the client side (M11): only requests to agent_api carry it."""

from __future__ import annotations

import httpx
import pytest
from agent_runtime.api_auth import api_headers
from agent_runtime.hitl_client import HITLClient
from agent_runtime.memory import MemoryClient


def test_api_headers_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_RUNTIME_API_TOKEN", raising=False)
    assert api_headers() == {}
    monkeypatch.setenv("AGENT_RUNTIME_API_TOKEN", "tok")
    assert api_headers() == {"Authorization": "Bearer tok"}


def _recording() -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/hitl":
            return httpx.Response(200, json={"request_id": 1})
        return httpx.Response(200, json={"episodes": [], "id": 1, "episode_id": 1})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def test_hitl_client_sends_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_API_TOKEN", "tok")
    http, seen = _recording()
    async with http:
        await HITLClient(http, "http://api").create("merge", "s", {})
    assert seen[0].headers["authorization"] == "Bearer tok"


async def test_memory_client_sends_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_API_TOKEN", "tok")
    http, seen = _recording()
    async with http:
        await MemoryClient(http, "http://api").slice("o/r", ["src/**"], "task")
    assert seen and all(r.headers["authorization"] == "Bearer tok" for r in seen)
