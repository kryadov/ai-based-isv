"""TrackerClient: plain-HTTP JSON-RPC to the task-source MCP server (M4c Task 5)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from agent_runtime.tracker import TrackerClient, TrackerError

URL = "http://tracker.test/mcp"


def _ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "{}"}],
                "isError": False,
                "structuredContent": payload,
            },
        },
    )


async def test_set_status_sends_one_stateless_tools_call() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok({"status": "in-progress"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await TrackerClient(http, URL).set_status("o/r#5", "in-progress", "claimed")
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == URL
    # FastMCP stateless streamable HTTP needs both media types accepted (spike).
    assert "application/json" in req.headers["Accept"]
    assert "text/event-stream" in req.headers["Accept"]
    body = json.loads(req.content)
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "tools/call"
    assert body["params"] == {
        "name": "set_task_status",
        "arguments": {"task_ref": "o/r#5", "status": "in-progress", "note": "claimed"},
    }


async def test_add_comment_calls_add_task_comment() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok({"commented": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await TrackerClient(http, URL).add_comment("o/r#5", "hello")
    assert seen[0]["params"] == {
        "name": "add_task_comment",
        "arguments": {"task_ref": "o/r#5", "body": "hello"},
    }


@pytest.mark.parametrize("status", ["open", "wontfix", "closed", ""])
async def test_only_platform_transitions_are_allowed(status: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("no request may be sent for a non-platform status")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError):
            await TrackerClient(http, URL).set_status("o/r#5", status)


async def test_tool_error_result_raises_tracker_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "issue not found"}],
                    "isError": True,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TrackerError, match="issue not found"):
            await TrackerClient(http, URL).set_status("o/r#5", "done")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}}
        ),
        httpx.Response(200, text="not json"),
    ],
)
async def test_transport_and_protocol_errors_raise_tracker_error(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TrackerError):
            await TrackerClient(http, URL).set_status("o/r#5", "blocked", "x")


async def test_connection_error_raises_tracker_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(TrackerError):
            await TrackerClient(http, URL).add_comment("o/r#5", "x")
