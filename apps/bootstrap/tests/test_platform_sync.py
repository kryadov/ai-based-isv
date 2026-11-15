"""platform-sync.sh: GitHub (canonical) ⇄ Gitea working copy (self-dev level 2).

Runs the real script against two local bare repos standing in for GitHub and
Gitea; needs only git and bash.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "infra" / "scripts" / "platform-sync.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash"
)


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _commit(work: Path, name: str) -> str:
    (work / name).write_text(name, encoding="utf-8")
    _git(work, "add", name)
    _git(work, "commit", "-qm", f"add {name}")
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> dict[str, Path]:
    gh = tmp_path / "gh.git"
    gt = tmp_path / "gt.git"
    work = tmp_path / "work"
    for bare in (gh, gt):
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    _commit(work, "base.txt")
    _git(work, "push", "-q", str(gh), "main")
    _git(work, "push", "-q", str(gt), "main")
    return {"gh": gh, "gt": gt, "work": work, "tmp": tmp_path}


def _sync(repos: dict[str, Path], cmd: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "SYNC_GITHUB_URL": str(repos["gh"]),
        "SYNC_GITEA_URL": str(repos["gt"]),
        "SYNC_GITEA_TOKEN": "unused-in-tests",
        "SYNC_NO_PR": "1",
        "SYNC_WORKDIR": str(repos["tmp"] / "sync"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), cmd], env=env, capture_output=True, text=True, check=False
    )


def _main(bare: Path) -> str:
    return _git(bare, "rev-parse", "refs/heads/main")


def test_pull_fast_forwards_gitea(repos: dict[str, Path]) -> None:
    head = _commit(repos["work"], "gh-only.txt")
    _git(repos["work"], "push", "-q", str(repos["gh"]), "main")
    result = _sync(repos, "pull")
    assert result.returncode == 0, result.stderr
    assert _main(repos["gt"]) == head


def test_pull_refuses_unpromoted_gitea_commits(repos: dict[str, Path]) -> None:
    head = _commit(repos["work"], "gitea-only.txt")
    _git(repos["work"], "push", "-q", str(repos["gt"]), "main")
    before = _main(repos["gt"])
    result = _sync(repos, "pull")
    assert result.returncode != 0
    assert "unpromoted" in result.stderr
    assert _main(repos["gt"]) == before == head


def test_promote_pushes_selfdev_branch(repos: dict[str, Path]) -> None:
    gh_before = _main(repos["gh"])
    head = _commit(repos["work"], "agent-change.txt")
    _git(repos["work"], "push", "-q", str(repos["gt"]), "main")
    result = _sync(repos, "promote")
    assert result.returncode == 0, result.stderr
    branch = _git(repos["gh"], "rev-parse", f"refs/heads/selfdev/{head[:7]}")
    assert branch == head
    assert _main(repos["gh"]) == gh_before  # never pushes GitHub main


def test_promote_noop_when_in_sync(repos: dict[str, Path]) -> None:
    result = _sync(repos, "promote")
    assert result.returncode == 0, result.stderr
    assert "nothing to promote" in result.stdout
    branches = _git(repos["gh"], "for-each-ref", "--format=%(refname)", "refs/heads/selfdev")
    assert branches == ""


def test_unknown_command_fails(repos: dict[str, Path]) -> None:
    assert _sync(repos, "bogus").returncode == 2
