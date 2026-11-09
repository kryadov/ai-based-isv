"""Tests for the thin opencode serve HTTP client."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from agent_runtime.opencode_client import OpencodeClient, OpencodeError


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    method = request.method

    if path == "/api/session" and method == "POST":
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "ses_test",
                    "projectID": "proj_test",
                    "model": {"id": "qwen-coder", "providerID": "litellm"},
                    "location": {"directory": "/workspace"},
                }
            },
        )

    if path == "/api/session/ses_test/prompt" and method == "POST":
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "msg_test",
                    "sessionID": "ses_test",
                    "type": "user",
                    "payload": {"text": "hello"},
                }
            },
        )

    if path == "/api/session/ses_bad/prompt" and method == "POST":
        return httpx.Response(
            400,
            json={"_tag": "InvalidRequestError", "message": "Expected Model.Ref | null"},
        )

    if path == "/api/event" and method == "GET":
        event_other = json.dumps(
            {
                "id": "evt_other",
                "type": "session.created",
                "data": {"sessionID": "ses_other"},
            }
        )
        event_1 = json.dumps(
            {
                "id": "evt_1",
                "type": "session.text.delta",
                "data": {"sessionID": "ses_test", "delta": "hi"},
            }
        )
        event_2 = json.dumps(
            {
                "id": "evt_2",
                "type": "session.execution.succeeded",
                "data": {"sessionID": "ses_test"},
            }
        )
        sse_body = (
            ": heartbeat\n"
            "\n"
            f"data: {event_other}\n"
            "\n"
            "this is garbage and should be ignored\n"
            f"data: {event_1}\n"
            "\n"
            ": heartbeat\n"
            "\n"
            f"data: {event_2}\n"
            "\n"
        )
        return httpx.Response(
            200,
            content=sse_body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    if path == "/api/session/ses_test/diff" and method == "GET":
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "file": "README.md",
                        "patch": (
                            "diff --git a/README.md b/README.md\n"
                            "--- a/README.md\n"
                            "+++ b/README.md\n"
                            "@@ -1 +1 @@\n"
                            "-old\n"
                            "+new\n"
                        ),
                        "additions": 1,
                        "deletions": 1,
                        "status": "modified",
                    }
                ]
            },
        )

    return httpx.Response(404)


@pytest.fixture
async def client() -> AsyncIterator[OpencodeClient]:
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(
        base_url="http://localhost:8080", transport=transport
    ) as http:
        yield OpencodeClient(http)


async def test_create_session_returns_id(client: OpencodeClient) -> None:
    session_id = await client.create_session()
    assert session_id == "ses_test"


async def test_send_prompt_posts_message(client: OpencodeClient) -> None:
    await client.send_prompt("ses_test", "hello")


async def test_events_parses_sse_stream(client: OpencodeClient) -> None:
    events = [event async for event in client.events("ses_test")]

    assert len(events) == 2
    assert [event["type"] for event in events] == [
        "session.text.delta",
        "session.execution.succeeded",
    ]
    assert events[0]["data"]["sessionID"] == "ses_test"
    assert events[0]["data"]["delta"] == "hi"
    assert events[1]["data"]["sessionID"] == "ses_test"


async def test_diff_returns_text(client: OpencodeClient) -> None:
    diff = await client.diff("ses_test")

    assert "diff --git a/README.md b/README.md" in diff
    assert "+new" in diff


async def test_http_error_raises_opencode_error(client: OpencodeClient) -> None:
    with pytest.raises(OpencodeError) as exc_info:
        await client.send_prompt("ses_bad", "hello")

    assert "InvalidRequestError" in str(exc_info.value)
