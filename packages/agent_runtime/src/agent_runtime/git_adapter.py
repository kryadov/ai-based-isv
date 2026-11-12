"""The worker's gitops in the shape the git MCP server consumes (``GitOpsLike``).

Structural typing only: ``agent_runtime`` does not depend on ``mcp_servers`` or
fastmcp. The adapter holds the Gitea credential; the MCP server never sees it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_runtime.gitops import GitOperationError, push_diff


class WorkerGitOps:
    """Pristine-clone git operations of one worker, credential held in-process."""

    def __init__(
        self,
        repo_dir: Path,
        *,
        repo_url: str,
        base_branch: str,
        token: str | None = None,
    ) -> None:
        self._repo_dir = repo_dir
        self._repo_url = repo_url
        self._base_branch = base_branch
        self._token = token

    def __repr__(self) -> str:
        return f"WorkerGitOps(repo_dir={self._repo_dir!s}, repo_url={self._repo_url!r})"

    def clone_status(self) -> dict[str, str]:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self._repo_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitOperationError(f"cannot read HEAD of {self._repo_dir}")
        return {
            "repo_url": self._repo_url,
            "base_branch": self._base_branch,
            "head": result.stdout.strip(),
        }

    def apply_and_push(self, diff: str, work_branch: str, message: str) -> str:
        return push_diff(self._repo_dir, diff, work_branch, message=message, token=self._token)
