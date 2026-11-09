"""Tests for the thin opencode serve HTTP client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import cast

import httpx
import pytest
from agent_runtime.opencode_client import OpencodeClient, OpencodeError, _EventStream


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


async def test_create_session_with_overrides(client: OpencodeClient) -> None:
    session_id = await client.create_session(
        model={"id": "custom-model", "providerID": "custom-provider"},
        directory="/custom/workspace",
    )
    assert session_id == "ses_test"


async def test_send_prompt_posts_message(client: OpencodeClient) -> None:
    await client.send_prompt("ses_test", "hello")


async def test_events_parses_sse_stream(client: OpencodeClient) -> None:
    events = [event async for event in (await client.events("ses_test"))]

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


async def test_streaming_http_error_raises_opencode_error() -> None:
    """Regression: non-2xx on the SSE endpoint must raise OpencodeError.

    MockTransport pre-populates response content, so it does not reproduce the
    ``httpx.ResponseNotRead`` behaviour that occurs on a real streaming
    response. This transport returns a response with an unread stream body.
    """

    class _UnreadStream(httpx.AsyncByteStream):
        def __init__(self, content: bytes) -> None:
            self._content = content

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield self._content

        async def aclose(self) -> None:
            pass

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/event" and request.method == "GET":
            return httpx.Response(
                401,
                stream=_UnreadStream(b'{"error":"unauthorized"}'),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="http://localhost:8080", transport=transport
    ) as http:
        client = OpencodeClient(http)
        with pytest.raises(OpencodeError) as exc_info:
            async for _event in (await client.events("ses_test")):
                pass

    assert "unauthorized" in str(exc_info.value)


async def test_get_session_returns_data(client: OpencodeClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session/ses_test" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "ses_test",
                        "outcome": "succeeded",
                        "tokens": {"input": 3, "output": 5, "reasoning": 0},
                    }
                },
            )
        if request.url.path == "/api/session/ses_bad" and request.method == "GET":
            return httpx.Response(200, json={"data": "not-a-dict"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="http://localhost:8080", transport=transport
    ) as http:
        client = OpencodeClient(http)
        data = await client.get_session("ses_test")

    assert data["id"] == "ses_test"
    assert data["outcome"] == "succeeded"
    assert data["tokens"]["input"] == 3

    async with httpx.AsyncClient(
        base_url="http://localhost:8080", transport=transport
    ) as http:
        client = OpencodeClient(http)
        with pytest.raises(OpencodeError):
            await client.get_session("ses_bad")


async def test_event_stream_aclose_closes_response() -> None:
    class _TrackedStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            event = json.dumps(
                {"id": "evt_1", "type": "session.created", "data": {"sessionID": "ses_test"}}
            )
            yield f"data: {event}\n\n".encode()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    stream = _TrackedStream()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/event" and request.method == "GET":
            return httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="http://localhost:8080", transport=transport
    ) as http:
        client = OpencodeClient(http)
        events = cast(_EventStream, await client.events("ses_test"))
        await events.aclose()

    assert stream.closed
