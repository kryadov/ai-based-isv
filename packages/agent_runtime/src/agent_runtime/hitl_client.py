"""Worker-side client for the agent_api HITL inbox (M5 gate corridor).

Implements ``agent_runtime.gates.HITLPort``: file a request, poll its status.
"""

from __future__ import annotations

from typing import Any

import httpx

from agent_runtime.api_auth import api_headers


class HITLError(RuntimeError):
    """The HITL inbox call failed."""


class HITLClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    async def create(self, kind: str, subject: str, context: dict[str, Any]) -> int:
        data = await self._call(
            "POST", "/hitl", json={"kind": kind, "subject": subject, "context": context}
        )
        try:
            return int(data["request_id"])
        except (KeyError, TypeError, ValueError):
            raise HITLError("malformed HITL create response") from None

    async def status(self, request_id: int) -> str:
        data = await self._call("GET", f"/hitl/{request_id}")
        status = data.get("status") if isinstance(data, dict) else None
        if not isinstance(status, str):
            raise HITLError("malformed HITL status response")
        return status

    async def _call(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._http.request(
                method, f"{self._base}{path}", json=json, headers=api_headers()
            )
        except httpx.HTTPError as exc:
            raise HITLError(f"{method} {path} failed: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise HITLError(f"{method} {path} -> HTTP {response.status_code}")
        return response.json()
