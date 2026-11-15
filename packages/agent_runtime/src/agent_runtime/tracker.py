"""Tracker status loop client: plain HTTP JSON-RPC to the task-source MCP server.

Deliberately not fastmcp-dependent. The server runs FastMCP streamable HTTP in
stateless JSON mode (M4c spike), so one ``POST /mcp`` with a ``tools/call`` body
is a complete round-trip — no initialize handshake, no session id.

Only the platform's own transitions are sent: ``in-progress`` on claim,
``done`` on a passing verdict, ``blocked`` on failure.
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx

PLATFORM_STATUSES: frozenset[str] = frozenset({"in-progress", "done", "blocked"})
_ACCEPT = "application/json, text/event-stream"


class TrackerError(RuntimeError):
    """A tracker call failed (transport, HTTP, JSON-RPC or tool error)."""


class TrackerClient:
    """Calls the task-source MCP tools over one ``httpx.AsyncClient``."""

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._url = base_url
        self._ids = itertools.count(1)

    async def set_status(self, task_ref: str, status: str, note: str = "") -> None:
        if status not in PLATFORM_STATUSES:
            raise ValueError(
                f"status {status!r} is not a platform transition; "
                f"expected one of {sorted(PLATFORM_STATUSES)}"
            )
        await self._call("set_task_status", {"task_ref": task_ref, "status": status, "note": note})

    async def add_comment(self, task_ref: str, body: str) -> None:
        await self._call("add_task_comment", {"task_ref": task_ref, "body": body})

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        try:
            response = await self._http.post(
                self._url,
                json=payload,
                headers={"Accept": _ACCEPT, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise TrackerError(f"{tool}: tracker unreachable: {exc}") from None
        if response.status_code >= 400:
            raise TrackerError(f"{tool}: HTTP {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
        except ValueError:
            raise TrackerError(f"{tool}: non-JSON response") from None
        if not isinstance(data, dict):
            raise TrackerError(f"{tool}: malformed JSON-RPC response")
        if "error" in data:
            raise TrackerError(f"{tool}: JSON-RPC error: {data['error']}")
        result = data.get("result")
        if not isinstance(result, dict):
            raise TrackerError(f"{tool}: JSON-RPC response without result")
        if result.get("isError"):
            raise TrackerError(f"{tool}: {_text(result)}")
        return result


def _text(result: dict[str, Any]) -> str:
    parts = [
        str(item.get("text", ""))
        for item in result.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    return " ".join(parts) or "tool error"
