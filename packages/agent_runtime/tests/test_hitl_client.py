"""Worker-side HITL client against the agent_api contract (M5 Task 7)."""

from __future__ import annotations

import json

import httpx
import pytest
from agent_runtime.hitl_client import HITLClient, HITLError


def _handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path == "/hitl":
        body = json.loads(request.content)
        assert body == {"kind": "merge", "subject": "PR", "context": {"a": 1}}
        return httpx.Response(200, json={"request_id": 11})
    if request.method == "GET" and request.url.path == "/hitl/11":
        return httpx.Response(200, json={"id": 11, "status": "approved"})
    return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture
def client() -> HITLClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return HITLClient(http, "http://api")


async def test_hitl_client_round_trip(client: HITLClient) -> None:
    rid = await client.create("merge", "PR", {"a": 1})
    assert rid == 11
    assert await client.status(rid) == "approved"


async def test_hitl_client_unknown_request_raises(client: HITLClient) -> None:
    with pytest.raises(HITLError):
        await client.status(12)
