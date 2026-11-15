"""ci MCP server: read-only CI status and job logs of a PR (M5).

``pr_ref`` format: ``owner/repo!number``. Agents (fix attempts) and the worker
use it to see why a PR's checks failed. The server has NO write tools; the
Gitea token is held in memory only and never reaches a tool result.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

_PR_REF_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)!([1-9][0-9]*)$")
_JOB_RE = re.compile(r"/actions/runs/\d+/jobs/(\d+)")
_REDACTED = "***"


def parse_pr_ref(pr_ref: str) -> tuple[str, str, int]:
    """Split ``owner/repo!N`` into its parts; raise ValueError otherwise."""
    match = _PR_REF_RE.match(pr_ref)
    if match is None:
        raise ValueError(f"invalid pr_ref {pr_ref!r}; expected owner/repo!number")
    return match.group(1), match.group(2), int(match.group(3))


class _Gitea:
    def __init__(self, http: httpx.AsyncClient, base_url: str, token: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._token = token

    async def get(self, path: str) -> httpx.Response:
        try:
            response = await self._http.get(
                f"{self._base}/api/v1{path}", headers={"Authorization": f"token {self._token}"}
            )
        except httpx.HTTPError as exc:
            raise ToolError(self._redact(f"GET {path} failed: {exc}")) from None
        if response.status_code >= 400:
            raise ToolError(self._redact(f"GET {path} -> HTTP {response.status_code}"))
        return response

    def _redact(self, text: str) -> str:
        return text.replace(self._token, _REDACTED) if self._token else text

    async def statuses(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        pr = (await self.get(f"/repos/{owner}/{repo}/pulls/{number}")).json()
        sha = str((pr.get("head") or {}).get("sha", ""))
        if not sha:
            raise ToolError("pull request has no head sha")
        data = (await self.get(f"/repos/{owner}/{repo}/commits/{sha}/status")).json()
        statuses = [
            {
                "context": str(s.get("context", "")),
                "state": str(s.get("status") or s.get("state") or ""),
                "target_url": str(s.get("target_url", "")),
            }
            for s in data.get("statuses") or []
            if isinstance(s, dict)
        ]
        state = str(data.get("state") or "pending") if statuses else "pending"
        return {"state": state, "statuses": statuses}


def create_ci_app(*, base_url: str, token: str, http: httpx.AsyncClient | None = None) -> FastMCP:
    gitea = _Gitea(http or httpx.AsyncClient(timeout=30.0), base_url, token)
    mcp = FastMCP("ci")

    def _ref(pr_ref: str) -> tuple[str, str, int]:
        try:
            return parse_pr_ref(pr_ref)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @mcp.tool
    async def get_pr_checks(pr_ref: str) -> dict[str, Any]:
        """Combined CI state of the PR head and every reported status check."""
        owner, repo, number = _ref(pr_ref)
        logger.info("get_pr_checks %s", pr_ref)
        return await gitea.statuses(owner, repo, number)

    @mcp.tool
    async def get_job_log(pr_ref: str, context: str, tail_lines: int = 200) -> str:
        """The last ``tail_lines`` lines of the CI job behind status ``context``."""
        owner, repo, number = _ref(pr_ref)
        logger.info("get_job_log %s %s", pr_ref, context)
        checks = await gitea.statuses(owner, repo, number)
        match = next(
            (
                _JOB_RE.search(s["target_url"])
                for s in checks["statuses"]
                if s["context"] == context
            ),
            None,
        )
        if match is None:
            return f"log unavailable: no job link for status {context!r}"
        try:
            text = (
                await gitea.get(f"/repos/{owner}/{repo}/actions/jobs/{match.group(1)}/logs")
            ).text
        except ToolError as exc:
            return f"log unavailable: {exc}"
        lines = text.splitlines()
        return "\n".join(lines[-max(1, min(tail_lines, 2000)) :])

    return mcp


def main() -> None:
    """Serve over streamable HTTP. Config from env: GITEA_BASE_URL, GITEA_TOKEN, PORT."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = create_ci_app(base_url=os.environ["GITEA_BASE_URL"], token=os.environ["GITEA_TOKEN"])
    # FastMCP (not Flask) bound in a pod; the NetworkPolicy is the boundary.
    # nosemgrep: python.flask.security.audit.app-run-param-config.avoid_app_run_with_bad_host
    app.run(
        transport="http",
        host="0.0.0.0",  # noqa: S104 - in-cluster service, fronted by NetworkPolicy
        port=int(os.environ.get("PORT", "8000")),
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
