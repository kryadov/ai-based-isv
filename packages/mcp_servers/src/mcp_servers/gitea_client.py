"""Thin async client for the Gitea Issues API (tracker v0).

The token travels only in the ``Authorization: token …`` header. Every error
raised from this module is a :class:`GiteaError` whose message has the token
redacted — upstream bodies and transport errors may echo request data back.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel

IssueState = Literal["open", "closed"]
_STATES: frozenset[str] = frozenset({"open", "closed"})
_REDACTED = "***"


class GiteaError(RuntimeError):
    """A Gitea API call failed; the message never contains the token."""


class IssueSummary(BaseModel):
    """An issue as listed: identity, title, state, link."""

    number: int
    title: str
    state: str
    url: str = ""


class IssueDetail(IssueSummary):
    """A single issue including its body (the task description)."""

    body: str = ""


class GiteaIssuesClient:
    """Issues operations for one Gitea instance, authenticated with ``token``."""

    def __init__(self, http: httpx.AsyncClient, *, base_url: str, token: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._token = token

    async def list_issues(self, owner: str, repo: str, state: str = "open") -> list[IssueSummary]:
        data = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "type": "issues", "limit": "50"},
        )
        if not isinstance(data, list):
            raise GiteaError("malformed issue list response")
        return [_summary(item) for item in data]

    async def get_issue(self, owner: str, repo: str, number: int) -> IssueDetail:
        data = await self._request("GET", f"/repos/{owner}/{repo}/issues/{number}")
        if not isinstance(data, dict):
            raise GiteaError("malformed issue response")
        summary = _summary(data)
        return IssueDetail(**summary.model_dump(), body=str(data.get("body") or ""))

    async def set_state(self, owner: str, repo: str, number: int, state: str) -> None:
        if state not in _STATES:
            raise ValueError(f"unknown issue state {state!r}; expected open|closed")
        await self._request(
            "PATCH", f"/repos/{owner}/{repo}/issues/{number}", json={"state": state}
        )

    async def add_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        await self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body}
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}/api/v1{path}"
        try:
            response = await self._http.request(
                method,
                url,
                params=params,
                json=json,
                headers={"Authorization": f"token {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise GiteaError(self._redact(f"{method} {path} failed: {exc}")) from None
        if response.status_code >= 400:
            raise GiteaError(
                self._redact(
                    f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}"
                )
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise GiteaError(f"{method} {path} returned non-JSON") from None

    def _redact(self, text: str) -> str:
        return text.replace(self._token, _REDACTED) if self._token else text


def _summary(item: Any) -> IssueSummary:
    if not isinstance(item, dict):
        raise GiteaError("malformed issue entry")
    try:
        return IssueSummary(
            number=int(item["number"]),
            title=str(item.get("title", "")),
            state=str(item.get("state", "")),
            url=str(item.get("html_url", "")),
        )
    except (KeyError, TypeError, ValueError):
        raise GiteaError("malformed issue entry") from None
