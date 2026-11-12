"""Thin HTTP client for ``opencode serve``.

The client is deliberately low-level: it maps the verified REST+SSE surface
(see ``docs/superpowers/research/2026-11-09-m4a-opencode-serve-spike.md``)
onto async methods and leaves session lifecycle policy to the adapter.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx


class OpencodeError(RuntimeError):
    """Non-2xx response or unrecoverable protocol error from opencode serve."""


_SSE_READ_TIMEOUT_SECONDS = 600.0


class _EventStream:
    """Eagerly-opened async iterator over the global opencode SSE stream.

    The stream is opened when ``_open()`` is awaited, before the caller posts
    a prompt. This prevents missing terminal events on the non-replaying
    global ``/api/event`` endpoint.
    """

    def __init__(self, http: httpx.AsyncClient, session_id: str) -> None:
        self._http = http
        self._session_id = session_id
        self._stream: AbstractAsyncContextManager[httpx.Response] | None = None
        self._response: httpx.Response | None = None
        self._line_iterator: AsyncIterator[str] | None = None
        self._buffer: list[str] = []

    async def _open(self) -> None:
        self._stream = self._http.stream(
            "GET",
            "/api/event",
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(_SSE_READ_TIMEOUT_SECONDS, read=_SSE_READ_TIMEOUT_SECONDS),
        )
        self._response = await self._stream.__aenter__()
        await _raise_for_status(self._response)
        self._line_iterator = self._response.aiter_lines().__aiter__()

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._line_iterator is None:
            raise OpencodeError("Event stream was not opened")

        while True:
            try:
                line = await self._line_iterator.__anext__()
            except StopAsyncIteration:
                await self._close()
                raise
            except BaseException:
                await self._close()
                raise

            if line.startswith(":"):
                continue
            if not line:
                if self._buffer:
                    payload = "\n".join(self._buffer)
                    self._buffer = []
                    try:
                        event: dict[str, Any] = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if event.get("data", {}).get("sessionID") == self._session_id:
                        return event
                continue
            if line.startswith("data:"):
                self._buffer.append(line[len("data:") :].lstrip(" "))

    async def _close(self) -> None:
        if self._stream is not None:
            await self._stream.__aexit__(None, None, None)
            self._stream = None
            self._response = None
            self._line_iterator = None

    async def aclose(self) -> None:
        """Explicitly close the SSE stream."""
        await self._close()


class OpencodeClient:
    """Minimal async client for the opencode serve HTTP API.

    Authentication and base URL are expected to be pre-configured on the
    supplied ``httpx.AsyncClient`` (Basic auth via ``auth=`` and/or base URL).
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def create_session(
        self,
        *,
        model: dict[str, str] | None = None,
        directory: str | None = None,
    ) -> str:
        """Create a new session and return its identifier.

        By default the request body is an empty JSON object, so the server uses
        the model and workspace configured in its ``opencode.jsonc``. Pass
        ``model`` and/or ``directory`` only when you need to override those
        defaults. The body is always a JSON object: opencode serve 2.2.0 answers
        a *bodyless* request with ``400 InvalidRequestError: Expected object``.
        """
        body: dict[str, Any] = {}
        if model is not None:
            body["model"] = model
        if directory is not None:
            body["location"] = {"directory": directory}
        response = await self._http.post("/api/session", json=body)
        await _raise_for_status(response)
        payload = response.json()
        return str(payload["data"]["id"])

    async def send_prompt(self, session_id: str, text: str) -> None:
        """Send a user prompt to the given session."""
        response = await self._http.post(
            f"/api/session/{session_id}/prompt",
            json={"text": text},
        )
        await _raise_for_status(response)

    async def mcp_status(self) -> list[dict[str, Any]]:
        """Return the harness's MCP servers and their connection status.

        opencode connects configured servers lazily at session start, so call
        this after a session exists (M4c spike).
        """
        response = await self._http.get("/api/mcp")
        await _raise_for_status(response)
        data = response.json().get("data")
        if not isinstance(data, list):
            raise OpencodeError("Malformed MCP status response")
        return [item for item in data if isinstance(item, dict)]

    async def get_session(self, session_id: str) -> dict[str, Any]:
        """Fetch the session status object (including token counts)."""
        response = await self._http.get(f"/api/session/{session_id}")
        await _raise_for_status(response)
        payload = response.json()
        data = payload.get("data")
        if not isinstance(data, dict):
            raise OpencodeError("Malformed session status response")
        return data

    async def events(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        """Return an async iterator over SSE events belonging to ``session_id``.

        The ``/api/event`` stream is global and does not replay events. The
        stream is opened *before* this coroutine returns, so callers must
        await this method before calling ``send_prompt``:

            events = await client.events(session_id)
            await client.send_prompt(session_id, text)
            async for event in events:
                ...

        Unknown event types are passed through; malformed lines and ``:``
        comments are tolerated. The read timeout is set to 600 seconds to
        survive long idle periods between events.
        """
        stream = _EventStream(self._http, session_id)
        await stream._open()
        return stream

    async def diff(self, session_id: str) -> str:
        """Return the unified diff text for ``session_id``.

        The endpoint returns a list of file changes under ``data``; this method
        joins the per-file ``patch`` strings with newlines. When no files
        changed (or git is unavailable in the container) the result is an
        empty string.
        """
        response = await self._http.get(f"/api/session/{session_id}/diff")
        await _raise_for_status(response)
        payload = response.json()
        patches = [str(item["patch"]) for item in payload.get("data", []) if item.get("patch")]
        return "\n".join(patches)


async def _raise_for_status(response: httpx.Response) -> None:
    """Raise ``OpencodeError`` for non-2xx responses.

    Works for both regular and streaming responses: streaming responses are
    read into memory before the error message is built.
    """
    if response.status_code >= 400:
        await response.aread()
        raise OpencodeError(response.text)
