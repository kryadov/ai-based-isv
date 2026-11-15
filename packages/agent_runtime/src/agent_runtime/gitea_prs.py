"""Thin async client for Gitea pull requests, commit statuses and reviews (M5).

The gate corridor (``agent_runtime.gates``) drives a PR through CI, review and
merge with these calls. The token travels only in the ``Authorization: token``
header; every error raised here is a :class:`GiteaPRError` with the token
redacted (upstream bodies may echo request data back).
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel

CIState = Literal["success", "failure", "error", "pending"]
ReviewEvent = Literal["COMMENT", "REQUEST_CHANGES", "APPROVED"]
_CI_STATES: frozenset[str] = frozenset({"success", "failure", "error", "pending"})
_REDACTED = "***"


class GiteaPRError(RuntimeError):
    """A Gitea PR API call failed; the message never contains the token."""


class PullRequest(BaseModel):
    number: int
    url: str
    head_sha: str
    state: str
    merged: bool


class ReviewComment(BaseModel):
    path: str
    line: int
    body: str


class GiteaPRClient:
    """PR operations for one Gitea instance, authenticated with ``token``."""

    def __init__(self, http: httpx.AsyncClient, *, base_url: str, token: str) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._token = token

    async def ensure_pr(
        self, owner: str, repo: str, *, head: str, base: str, title: str, body: str
    ) -> PullRequest:
        """The open PR for ``head`` if one exists (a retried vertex), else a new one."""
        open_prs = await self._request(
            "GET", f"/repos/{owner}/{repo}/pulls", params={"state": "open", "limit": "50"}
        )
        if not isinstance(open_prs, list):
            raise GiteaPRError("malformed pull request list response")
        for item in open_prs:
            if isinstance(item, dict) and (item.get("head") or {}).get("ref") == head:
                return _pr(item)
        created = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"head": head, "base": base, "title": title, "body": body},
        )
        return _pr(created)

    async def get_pr(self, owner: str, repo: str, number: int) -> PullRequest:
        return _pr(await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}"))

    async def combined_status(self, owner: str, repo: str, sha: str) -> CIState:
        """Combined commit status; no statuses reported yet counts as ``pending``."""
        data = await self._request("GET", f"/repos/{owner}/{repo}/commits/{sha}/status")
        if not isinstance(data, dict):
            raise GiteaPRError("malformed commit status response")
        if not data.get("statuses"):
            return "pending"
        state = str(data.get("state") or "pending")
        if state not in _CI_STATES:
            # Unknown states never count as green.
            return "error"
        return state  # type: ignore[return-value]  # narrowed by _CI_STATES

    async def changed_files(self, owner: str, repo: str, number: int) -> list[str]:
        data = await self._request(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}/files", params={"limit": "500"}
        )
        if not isinstance(data, list):
            raise GiteaPRError("malformed changed files response")
        return [str(item["filename"]) for item in data if isinstance(item, dict)]

    async def pr_diff(self, owner: str, repo: str, number: int) -> str:
        return await self._request_text("GET", f"/repos/{owner}/{repo}/pulls/{number}.diff")

    async def post_review(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        body: str,
        comments: list[ReviewComment],
        event: ReviewEvent,
    ) -> None:
        await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            json={
                "body": body,
                "event": event,
                "comments": [
                    {"path": c.path, "new_position": c.line, "body": c.body} for c in comments
                ],
            },
        )

    async def comment(self, owner: str, repo: str, number: int, body: str) -> None:
        await self._request(
            "POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body}
        )

    async def merge(self, owner: str, repo: str, number: int, *, message: str) -> None:
        await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/merge",
            json={"Do": "merge", "merge_message_field": message},
        )

    async def close(self, owner: str, repo: str, number: int) -> None:
        await self._request(
            "PATCH", f"/repos/{owner}/{repo}/pulls/{number}", json={"state": "closed"}
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
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
            raise GiteaPRError(self._redact(f"{method} {path} failed: {exc}")) from None
        if response.status_code >= 400:
            raise GiteaPRError(
                self._redact(
                    f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}"
                )
            )
        return response

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        response = await self._send(method, path, params=params, json=json)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise GiteaPRError(f"{method} {path} returned non-JSON") from None

    async def _request_text(self, method: str, path: str) -> str:
        return (await self._send(method, path)).text

    def _redact(self, text: str) -> str:
        return text.replace(self._token, _REDACTED) if self._token else text


def _pr(item: Any) -> PullRequest:
    if not isinstance(item, dict):
        raise GiteaPRError("malformed pull request")
    try:
        head = item.get("head") or {}
        return PullRequest(
            number=int(item["number"]),
            url=str(item.get("html_url", "")),
            head_sha=str(head.get("sha", "")),
            state=str(item.get("state", "")),
            merged=bool(item.get("merged", False)),
        )
    except (KeyError, TypeError, ValueError):
        raise GiteaPRError("malformed pull request") from None
