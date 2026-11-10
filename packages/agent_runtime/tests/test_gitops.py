"""Worker-side git: clone/reset a workspace copy and push the agent's diff as a branch.

These tests drive real ``git`` against tmp bare remotes (git is available on this
machine). They cover the two interfaces the Task 9 worker relies on:

* :func:`ensure_repo` — keep a fresh clone at the base branch (clone if absent, else
  ``fetch`` + hard reset onto ``origin/<base>``), discarding any local divergence;
* :func:`push_diff` — apply the agent's diff to that clean clone, commit it on a work
  branch and push the branch, returning the new commit sha.

The security-critical property under test is that a Gitea access token never lands in
argv, in ``.git/config`` or in an exception message: it is passed to ``git`` only
through the process environment (``http.extraHeader`` via ``GIT_CONFIG_*``).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from agent_runtime.gitops import (
    AGENT_RUNTIME_EMAIL,
    AGENT_RUNTIME_NAME,
    GitOperationError,
    _reject_credentialed_url,
    apply_diff,
    clone_local,
    ensure_repo,
    push_diff,
    push_head,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_FIXTURE_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
}


def _git(args: list[str], cwd: Path, stdin: str | None = None) -> str:
    """Run a real git command for test-fixture setup; return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        env=_FIXTURE_ENV,
    )
    return result.stdout


def _bare_remote_with_main(tmp_path: Path) -> tuple[str, str]:
    """Create a bare remote whose ``main`` has README.txt == 'one\\ntwo\\n'.

    Returns ``(remote_url, base_sha)`` where ``base_sha`` is the sha of the sole
    ``main`` commit (the base the worker clones and resets onto).
    """
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-q", "-b", "main"], seed)
    (seed / "README.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(["add", "README.txt"], seed)
    _git(["commit", "-qm", "init"], seed)
    _git(["push", "-q", str(remote), "main"], seed)
    base_sha = _git(["rev-parse", "HEAD"], seed).strip()
    return str(remote), base_sha


def _clone_branch(remote_url: str, branch: str, dest: Path) -> Path:
    """Clone ``branch`` from the remote into ``dest`` (which must not exist yet)."""
    _git(["clone", "-q", "--branch", branch, remote_url, dest.name], dest.parent)
    return dest


def test_ensure_repo_clones_and_resets(tmp_path: Path) -> None:
    remote_url, base_sha = _bare_remote_with_main(tmp_path)
    workspace = tmp_path / "ws"

    repo_dir = ensure_repo(workspace, remote_url, "main")

    assert repo_dir == workspace / "remote"
    assert (repo_dir / ".git").is_dir()
    assert (repo_dir / "README.txt").read_text(encoding="utf-8") == "one\ntwo\n"

    # a local edit to a tracked file diverges the working tree from the base
    (repo_dir / "README.txt").write_text("one\ntwo\nLOCAL\n", encoding="utf-8")

    # the second call is a no-op clone (already present) that resets back onto base
    second = ensure_repo(workspace, remote_url, "main")
    assert second == repo_dir
    assert (second / "README.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert _git(["rev-parse", "HEAD"], second).strip() == base_sha


def test_ensure_repo_resets_committed_divergence(tmp_path: Path) -> None:
    remote_url, base_sha = _bare_remote_with_main(tmp_path)
    workspace = tmp_path / "ws"

    repo_dir = ensure_repo(workspace, remote_url, "main")

    # a real local commit on the checked-out branch must be discarded by the reset
    (repo_dir / "README.txt").write_text("one\ntwo\ncommitted-divergence\n", encoding="utf-8")
    _git(["add", "README.txt"], repo_dir)
    _git(["commit", "-qm", "local divergence"], repo_dir)
    assert _git(["rev-parse", "HEAD"], repo_dir).strip() != base_sha

    ensure_repo(workspace, remote_url, "main")
    assert _git(["rev-parse", "HEAD"], repo_dir).strip() == base_sha
    assert (repo_dir / "README.txt").read_text(encoding="utf-8") == "one\ntwo\n"


def test_push_diff_creates_branch_and_commit(tmp_path: Path) -> None:
    remote_url, base_sha = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")

    # capture a real, applyable unified diff by editing the clean clone then reverting
    (repo_dir / "README.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    diff = _git(["diff", "--", "README.txt"], repo_dir)
    assert diff.strip()
    _git(["checkout", "--", "README.txt"], repo_dir)
    assert not _git(["status", "--porcelain"], repo_dir).strip()

    sha = push_diff(repo_dir, diff, "agent/vertex-1", message="Add three")

    assert _SHA_RE.match(sha)
    assert sha == _git(["rev-parse", "HEAD"], repo_dir).strip()

    # verify against a fresh clone of the remote: branch content and its base parent
    branch_dir = _clone_branch(remote_url, "agent/vertex-1", tmp_path / "check")
    assert (branch_dir / "README.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    assert _git(["rev-parse", "HEAD^"], branch_dir).strip() == base_sha


def test_push_diff_uses_agent_runtime_identity(tmp_path: Path) -> None:
    remote_url, _ = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")

    (repo_dir / "README.txt").write_text("one\ntwo\nsigned\n", encoding="utf-8")
    diff = _git(["diff", "--", "README.txt"], repo_dir)
    _git(["checkout", "--", "README.txt"], repo_dir)

    push_diff(repo_dir, diff, "agent/vertex-2", message="identity check")

    fmt = "%an|%ae|%cn|%ce"
    log = _git(["log", "-1", f"--format={fmt}"], repo_dir).strip()
    author, aemail, committer, cemail = log.split("|")
    assert author == AGENT_RUNTIME_NAME
    assert committer == AGENT_RUNTIME_NAME
    assert aemail == AGENT_RUNTIME_EMAIL
    assert cemail == AGENT_RUNTIME_EMAIL


def test_push_diff_fails_loudly_on_dirty_tree(tmp_path: Path) -> None:
    remote_url, base_sha = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")

    (repo_dir / "README.txt").write_text("one\ntwo\ndirty\n", encoding="utf-8")
    diff = _git(["diff", "--", "README.txt"], repo_dir)
    # leave the tree dirty (do NOT revert) — push_diff must refuse rather than clobber
    assert _git(["status", "--porcelain"], repo_dir).strip()

    with pytest.raises(GitOperationError):
        push_diff(repo_dir, diff, "agent/vertex-3", message="should not run")

    # no side effects: the local commit was never made and the branch was never created
    assert _git(["rev-parse", "HEAD"], repo_dir).strip() == base_sha
    assert _git(["status", "--porcelain"], repo_dir).strip()


def test_push_diff_rejects_empty_diff(tmp_path: Path) -> None:
    remote_url, base_sha = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")

    with pytest.raises(GitOperationError):
        push_diff(repo_dir, "", "agent/vertex-4", message="nothing to apply")

    assert _git(["rev-parse", "HEAD"], repo_dir).strip() == base_sha


def test_push_diff_rejects_conflicting_diff(tmp_path: Path) -> None:
    remote_url, base_sha = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")

    # context that does not exist in the base file: git apply must fail, loudly
    conflicting = (
        "diff --git a/README.txt b/README.txt\n"
        "index 1111111..2222222 100644\n"
        "--- a/README.txt\n"
        "+++ b/README.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " one\n"
        "-NOT-PRESENT\n"
        "+oops\n"
    )

    with pytest.raises(GitOperationError):
        push_diff(repo_dir, conflicting, "agent/vertex-5", message="bad patch")

    # apply is atomic and runs before the branch move: no commit, no new branch, no
    # partial working-tree edit — the base clone is left exactly as it was found.
    assert _git(["rev-parse", "HEAD"], repo_dir).strip() == base_sha
    assert _git(["status", "--porcelain"], repo_dir).strip() == ""
    assert "agent/vertex-5" not in _git(["branch", "--list"], repo_dir)


def test_push_diff_overwrites_own_branch_on_retry(tmp_path: Path) -> None:
    remote_url, _ = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")
    first_repo = ensure_repo(tmp_path / "ws-first", remote_url, "main")

    (first_repo / "README.txt").write_text("one\ntwo\nfirst\n", encoding="utf-8")
    diff_one = _git(["diff", "--", "README.txt"], first_repo)
    _git(["checkout", "--", "README.txt"], first_repo)
    sha_one = push_diff(first_repo, diff_one, "agent/vertex-6", message="first attempt")

    # The retry clone pre-dates the remote work branch and fetches only main, so it has
    # no refs/remotes/origin/agent/vertex-6 tracking ref for --force-with-lease.
    ensure_repo(tmp_path / "ws", remote_url, "main")
    (repo_dir / "README.txt").write_text("one\ntwo\nsecond\n", encoding="utf-8")
    diff_two = _git(["diff", "--", "README.txt"], repo_dir)
    _git(["checkout", "--", "README.txt"], repo_dir)
    sha_two = push_diff(repo_dir, diff_two, "agent/vertex-6", message="retry attempt")

    assert sha_one != sha_two
    branch_dir = _clone_branch(remote_url, "agent/vertex-6", tmp_path / "retry-check")
    assert (branch_dir / "README.txt").read_text(encoding="utf-8") == "one\ntwo\nsecond\n"
    assert _git(["rev-parse", "HEAD"], branch_dir).strip() == sha_two


def test_token_is_never_stored_in_repo_config(tmp_path: Path) -> None:
    secret = "sup3r-s3cret-gitea-token"
    remote_url, _ = _bare_remote_with_main(tmp_path)

    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main", token=secret)

    (repo_dir / "README.txt").write_text("one\ntwo\ntoken\n", encoding="utf-8")
    diff = _git(["diff", "--", "README.txt"], repo_dir)
    _git(["checkout", "--", "README.txt"], repo_dir)
    push_diff(repo_dir, diff, "agent/vertex-7", message="with token", token=secret)

    stored = (repo_dir / ".git" / "config").read_text(encoding="utf-8")
    assert secret not in stored
    # the on-disk remote is exactly the clean URL we passed in — no credentials appended
    assert _git(["remote", "get-url", "origin"], repo_dir).strip() == remote_url


def test_token_never_leaks_into_exception_text(tmp_path: Path) -> None:
    """The security-critical property: a token passed to push_diff must never appear in
    the GitOperationError text, which embeds the git argv and the remote URL from stderr.
    Exercised for real by pushing to a reachable-then-broken remote so the failure path
    quotes argv (no token) and git's stderr (the URL, still credential-free)."""
    secret = "LEAK-CHECK-t0k3n"
    remote_url, _ = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main", token=secret)

    # stage a real, applyable diff on the clean base
    (repo_dir / "README.txt").write_text("one\ntwo\nleak\n", encoding="utf-8")
    diff = _git(["diff", "--", "README.txt"], repo_dir)
    _git(["checkout", "--", "README.txt"], repo_dir)

    # repoint the remote to an unroutable URL (no credentials) so the push must fail
    _git(["remote", "set-url", "origin", "http://127.0.0.1:1/nope.git"], repo_dir)
    with pytest.raises(GitOperationError) as exc_info:
        push_diff(repo_dir, diff, "agent/vertex-8", message="push fails", token=secret)

    assert secret not in str(exc_info.value)
    # and it never reached the on-disk remote either
    assert secret not in (repo_dir / ".git" / "config").read_text(encoding="utf-8")


def test_apply_diff_stages_changes_without_commit_or_branch(tmp_path: Path) -> None:
    remote_url, base_sha = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")

    (repo_dir / "README.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    diff = _git(["diff", "--", "README.txt"], repo_dir)
    _git(["checkout", "--", "README.txt"], repo_dir)
    assert not _git(["status", "--porcelain"], repo_dir).strip()

    apply_diff(repo_dir, diff)

    # changes are staged, no commit/branch yet
    status = _git(["status", "--porcelain"], repo_dir).strip()
    assert status.startswith("M  README.txt")
    assert _git(["rev-parse", "HEAD"], repo_dir).strip() == base_sha
    assert "agent/vertex-apply" not in _git(["branch", "--list"], repo_dir)


def test_push_head_commits_and_pushes_staged_changes(tmp_path: Path) -> None:
    remote_url, base_sha = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")

    (repo_dir / "README.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(["add", "README.txt"], repo_dir)

    sha = push_head(repo_dir, "agent/vertex-head", message="Add three")

    assert _SHA_RE.match(sha)
    assert sha == _git(["rev-parse", "HEAD"], repo_dir).strip()
    assert _git(["rev-parse", "HEAD^"], repo_dir).strip() == base_sha

    branch_dir = _clone_branch(remote_url, "agent/vertex-head", tmp_path / "check")
    assert (branch_dir / "README.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_apply_diff_rejects_empty_diff(tmp_path: Path) -> None:
    remote_url, base_sha = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")

    with pytest.raises(GitOperationError):
        apply_diff(repo_dir, "")

    assert _git(["rev-parse", "HEAD"], repo_dir).strip() == base_sha


def test_push_head_rejects_nothing_staged(tmp_path: Path) -> None:
    remote_url, base_sha = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")

    with pytest.raises(GitOperationError):
        push_head(repo_dir, "agent/vertex-empty", message="no changes")

    assert _git(["rev-parse", "HEAD"], repo_dir).strip() == base_sha


def test_clone_local_creates_independent_copy(tmp_path: Path) -> None:
    remote_url, base_sha = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")

    copy_dir = tmp_path / "copy"
    clone_local(repo_dir, copy_dir)

    assert (copy_dir / ".git").is_dir()
    assert (copy_dir / "README.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert _git(["rev-parse", "HEAD"], copy_dir).strip() == base_sha

    # Independence: further edits to the source do not affect the copy.
    (repo_dir / "README.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert (copy_dir / "README.txt").read_text(encoding="utf-8") == "one\ntwo\n"


def test_clone_local_refuses_existing_destination(tmp_path: Path) -> None:
    remote_url, _ = _bare_remote_with_main(tmp_path)
    repo_dir = ensure_repo(tmp_path / "ws", remote_url, "main")
    existing = tmp_path / "existing"
    existing.mkdir()

    with pytest.raises(GitOperationError):
        clone_local(repo_dir, existing)


def test_credentialed_repo_url_is_rejected(tmp_path: Path) -> None:
    """A token embedded in repo_url (standard git idiom) is the one thing this module must
    never accept: it would reach argv, exception text and .git/config. Fail closed — and
    the rejection itself must not echo the secret."""
    secret = "sup3r-s3cret-gitea-token"
    credentialed = f"http://oauth2:{secret}@gitea.invalid/owner/repo.git"
    workspace = tmp_path / "ws"

    with pytest.raises(GitOperationError) as exc_info:
        ensure_repo(workspace, credentialed, "main")

    assert secret not in str(exc_info.value)
    # fail-closed before any side effect: nothing was cloned or written
    assert not workspace.exists()

    # token-as-username form is rejected too
    with pytest.raises(GitOperationError):
        ensure_repo(tmp_path / "ws2", f"https://{secret}@gitea.invalid/o/r.git", "main")

    # the allow side of the rule: clean http(s), file, local path and scp-style ssh are
    # all fine (the ssh user is not a secret)
    for ok in [
        "https://gitea.invalid/o/r.git",
        "http://gitea.invalid/o/r.git",
        "file:///srv/git/r.git",
        "/srv/git/r.git",
        "git@github.com:owner/repo.git",
        "ssh://git@host/o/r.git",
    ]:
        _reject_credentialed_url(ok)  # must not raise
