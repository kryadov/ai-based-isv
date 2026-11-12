"""WorkerGitOps: the worker's gitops behind the git MCP server's GitOpsLike shape."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from agent_runtime.git_adapter import WorkerGitOps
from agent_runtime.gitops import GitOperationError, ensure_repo

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}

_DIFF = (
    "diff --git a/README.txt b/README.txt\n"
    "--- a/README.txt\n"
    "+++ b/README.txt\n"
    "@@ -1 +1,2 @@\n"
    " one\n"
    "+two\n"
)


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=_ENV
    ).stdout


def _remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-q", "-b", "main"], seed)
    (seed / "README.txt").write_text("one\n", encoding="utf-8")
    _git(["add", "README.txt"], seed)
    _git(["commit", "-qm", "init"], seed)
    _git(["push", "-q", str(remote), "main"], seed)
    return str(remote)


def test_clone_status_describes_the_pristine_clone(tmp_path: Path) -> None:
    remote = _remote(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote, "main")
    status = WorkerGitOps(repo_dir, repo_url=remote, base_branch="main").clone_status()
    assert status["repo_url"] == remote
    assert status["base_branch"] == "main"
    assert status["head"] == _git(["rev-parse", "HEAD"], repo_dir).strip()


def test_apply_and_push_pushes_the_work_branch(tmp_path: Path) -> None:
    remote = _remote(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote, "main")
    sha = WorkerGitOps(repo_dir, repo_url=remote, base_branch="main").apply_and_push(
        _DIFF, "agent/vertex-1", "agent: add two"
    )
    pushed = _git(["rev-parse", "refs/heads/agent/vertex-1"], Path(remote)).strip()
    assert sha == pushed


def test_apply_and_push_surfaces_git_errors(tmp_path: Path) -> None:
    remote = _remote(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote, "main")
    with pytest.raises(GitOperationError):
        WorkerGitOps(repo_dir, repo_url=remote, base_branch="main").apply_and_push(
            "", "agent/vertex-1", "m"
        )


def test_token_is_not_exposed_by_repr(tmp_path: Path) -> None:
    ops = WorkerGitOps(tmp_path, repo_url="http://g/o/r", base_branch="main", token="SECRET-T")
    assert "SECRET-T" not in repr(ops)
