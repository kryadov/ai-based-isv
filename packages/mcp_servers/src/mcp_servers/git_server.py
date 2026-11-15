"""git MCP server — WORKER-SIDE v0, audited.

The sandbox never receives git write credentials (M4a ruling). This server runs
in-process with the worker, which adapts its own gitops (and the credential it
holds) to the narrow :class:`GitOpsLike` protocol. ``mcp_servers`` therefore
does not depend on ``agent_runtime``. Agent-driven git through this server
waits for per-project scoped tokens (M5).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)


class GitOpsLike(Protocol):
    """The git operations the worker exposes; implementations hold the credential."""

    def clone_status(self) -> dict[str, str]:
        """Describe the pristine clone (repo, base branch, head sha)."""
        ...

    def apply_and_push(self, diff: str, work_branch: str, message: str) -> str:
        """Apply ``diff`` to the pristine clone, commit, push; return the new sha."""
        ...


def create_git_server(gitops: GitOpsLike) -> FastMCP:
    """Build the worker-side git server over ``gitops``."""
    mcp: FastMCP = FastMCP("git")

    @mcp.tool
    async def clone_status() -> dict[str, str]:
        """Describe the worker's pristine clone of the target repository."""
        try:
            return await asyncio.to_thread(gitops.clone_status)
        except Exception as exc:
            logger.info("op=clone_status outcome=error")
            raise ToolError(f"clone_status failed: {exc}") from None

    @mcp.tool
    async def apply_and_push(
        run_id: int, vertex_id: int, diff: str, work_branch: str, message: str
    ) -> dict[str, Any]:
        """Apply a diff, commit it on ``work_branch`` and push the branch."""
        try:
            sha = await asyncio.to_thread(gitops.apply_and_push, diff, work_branch, message)
        except Exception as exc:
            logger.info(
                "op=apply_and_push run=%s vertex=%s branch=%s outcome=error",
                run_id,
                vertex_id,
                work_branch,
            )
            raise ToolError(f"apply_and_push failed: {exc}") from None
        audit = f"run {run_id} vertex {vertex_id}: pushed {work_branch} at {sha}"
        logger.info(
            "op=apply_and_push run=%s vertex=%s branch=%s sha=%s outcome=ok",
            run_id,
            vertex_id,
            work_branch,
            sha,
        )
        return {
            "run_id": run_id,
            "vertex_id": vertex_id,
            "branch": work_branch,
            "sha": sha,
            "audit": audit,
        }

    return mcp
