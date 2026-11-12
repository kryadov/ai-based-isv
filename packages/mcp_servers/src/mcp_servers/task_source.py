"""task-source MCP server: the tracker (Gitea Issues, v0) as MCP tools.

``task_ref`` format: ``owner/repo#number``. Platform statuses map onto Gitea's
two issue states plus a status comment: ``done`` closes the issue; every other
status (``open``, ``in-progress``, ``blocked``) keeps it open. Every status
change posts a ``[platform] status: <status>`` comment, so the tracker shows
the platform's own transitions without inventing labels.

The admin token is held in memory only: it is never logged, and every error
that reaches a tool result comes from :class:`GiteaError`, whose message is
already redacted.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mcp_servers.gitea_client import GiteaError, GiteaIssuesClient, IssueSummary

logger = logging.getLogger(__name__)

TaskStatus = Literal["open", "in-progress", "done", "blocked"]
_TASK_REF_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)$")
_PREFIX_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#?$")


def parse_task_ref(task_ref: str) -> tuple[str, str, int]:
    """Split ``owner/repo#N`` into its parts; raise ValueError otherwise."""
    match = _TASK_REF_RE.match(task_ref)
    if match is None:
        raise ValueError(f"invalid task_ref {task_ref!r}; expected owner/repo#number")
    return match.group(1), match.group(2), int(match.group(3))


def status_comment(status: str, note: str = "") -> str:
    """The comment body that records a platform status transition."""
    body = f"[platform] status: {status}"
    return f"{body}\n\n{note}" if note else body


def create_task_source_app(
    *, base_url: str, token: str, http: httpx.AsyncClient | None = None
) -> FastMCP:
    """Build the task-source server over one Gitea instance.

    ``http`` is an injection seam for tests (``httpx.MockTransport``); by
    default the server owns a client with a 30 s timeout.
    """
    gitea = GiteaIssuesClient(
        http if http is not None else httpx.AsyncClient(timeout=30.0),
        base_url=base_url,
        token=token,
    )
    mcp: FastMCP = FastMCP("task-source")

    def _ref(task_ref: str) -> tuple[str, str, int]:
        try:
            return parse_task_ref(task_ref)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    def _audit(op: str, task_ref: str, outcome: str) -> None:
        logger.info("op=%s task_ref=%s outcome=%s", op, task_ref, outcome)

    @mcp.tool
    async def get_task(task_ref: str) -> dict[str, Any]:
        """Return one task (issue): task_ref, title, state, body, url."""
        owner, repo, number = _ref(task_ref)
        try:
            issue = await gitea.get_issue(owner, repo, number)
        except GiteaError as exc:
            _audit("get_task", task_ref, "error")
            raise ToolError(str(exc)) from None
        _audit("get_task", task_ref, "ok")
        return {"task_ref": task_ref, **issue.model_dump()}

    @mcp.tool
    async def list_tasks(task_ref_prefix: str) -> list[dict[str, Any]]:
        """List open tasks of a repository; task_ref_prefix is ``owner/repo``."""
        match = _PREFIX_RE.match(task_ref_prefix)
        if match is None:
            raise ToolError(f"invalid task_ref_prefix {task_ref_prefix!r}; expected owner/repo")
        owner, repo = match.group(1), match.group(2)
        try:
            issues = await gitea.list_issues(owner, repo)
        except GiteaError as exc:
            _audit("list_tasks", task_ref_prefix, "error")
            raise ToolError(str(exc)) from None
        _audit("list_tasks", task_ref_prefix, "ok")
        return [_with_ref(owner, repo, issue) for issue in issues]

    @mcp.tool
    async def set_task_status(task_ref: str, status: TaskStatus, note: str = "") -> dict[str, Any]:
        """Record a platform status: done closes the issue, others keep it open."""
        owner, repo, number = _ref(task_ref)
        state = "closed" if status == "done" else "open"
        try:
            await gitea.set_state(owner, repo, number, state)
            await gitea.add_comment(owner, repo, number, status_comment(status, note))
        except GiteaError as exc:
            _audit("set_task_status", task_ref, f"error status={status}")
            raise ToolError(str(exc)) from None
        _audit("set_task_status", task_ref, f"ok status={status}")
        return {"task_ref": task_ref, "status": status, "state": state}

    @mcp.tool
    async def add_task_comment(task_ref: str, body: str) -> dict[str, Any]:
        """Add a comment to the task (issue)."""
        owner, repo, number = _ref(task_ref)
        try:
            await gitea.add_comment(owner, repo, number, body)
        except GiteaError as exc:
            _audit("add_task_comment", task_ref, "error")
            raise ToolError(str(exc)) from None
        _audit("add_task_comment", task_ref, "ok")
        return {"task_ref": task_ref, "commented": True}

    return mcp


def _with_ref(owner: str, repo: str, issue: IssueSummary) -> dict[str, Any]:
    return {"task_ref": f"{owner}/{repo}#{issue.number}", **issue.model_dump()}


def main() -> None:
    """Serve over streamable HTTP; config from env (GITEA_BASE_URL, GITEA_TOKEN, PORT)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = create_task_source_app(
        base_url=os.environ["GITEA_BASE_URL"], token=os.environ["GITEA_TOKEN"]
    )
    app.run(
        transport="http",
        host="0.0.0.0",  # noqa: S104 - in-cluster service, fronted by NetworkPolicy
        port=int(os.environ.get("PORT", "8000")),
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
