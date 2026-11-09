"""Thin HTTP client for ``opencode serve``.

The client is deliberately low-level: it maps the verified REST+SSE surface
(see ``docs/superpowers/research/2026-11-09-m4a-opencode-serve-spike.md``)
onto async methods and leaves session lifecycle policy to the adapter.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


class OpencodeError(RuntimeError):
    """Non-2xx response or unrecoverable protocol error from opencode serve."""


class OpencodeClient:
    """Minimal async client for the opencode serve HTTP API.

    Authentication and base URL are expected to be pre-configured on the
    supplied ``httpx.AsyncClient`` (Basic auth via ``auth=`` and/or base URL).
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def create_session(self) -> str:
        """Create a new session and return its identifier."""
        response = await self._http.post(
            "/api/session",
            json={
                "model": {"id": "qwen-coder", "providerID": "litellm"},
                "location": {"directory": "/workspace"},
            },
        )
        self._raise_for_status(response)
        payload = response.json()
        return str(payload["data"]["id"])

    async def send_prompt(self, session_id: str, text: str) -> None:
        """Send a user prompt to the given session."""
        response = await self._http.post(
            f"/api/session/{session_id}/prompt",
            json={"text": text},
        )
        self._raise_for_status(response)

    async def events(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield SSE events belonging to ``session_id``.

        The ``/api/event`` stream is global, so the client filters by
        ``data.sessionID``. Unknown event types are passed through; malformed
        lines/comments are tolerated.
        """
        async with self._http.stream(
            "GET", "/api/event", headers={"Accept": "text/event-stream"}
        ) as response:
            self._raise_for_status(response)
            buffer: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith(":"):
                    continue
                if not line:
                    if buffer:
                        data = "\n".join(buffer)
                        buffer = []
                        try:
                            event: dict[str, Any] = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if event.get("data", {}).get("sessionID") == session_id:
                            yield event
                    continue
                if line.startswith("data:"):
                    buffer.append(line[len("data:") :].lstrip(" "))

    async def diff(self, session_id: str) -> str:
        """Return the unified diff text for ``session_id``.

        The endpoint returns a list of file changes under ``data``; this method
        joins the per-file ``patch`` strings with newlines. When no files
        changed (or git is unavailable in the container) the result is an
        empty string.
        """
        response = await self._http.get(f"/api/session/{session_id}/diff")
        self._raise_for_status(response)
        payload = response.json()
        patches = [
            str(item["patch"])
            for item in payload.get("data", [])
            if item.get("patch")
        ]
        return "\n".join(patches)

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise OpencodeError(response.text)
