"""Worker-side git: keep a fresh clone and push the agent's diff as a branch.

The sandbox (opencode) is deliberately given no git write credentials — see the design
decision in ``docs/superpowers/specs/2026-07-swarm-dev-platform-design.md`` §7.2 and
the plan's Task 8. The worker (Task 9) instead owns every git write:

* :func:`ensure_repo` keeps a clean copy of the target repo in the worker workspace
  (clone if absent, else ``fetch`` + hard reset onto ``origin/<base>`` so each vertex
  starts from a pristine base);
* :func:`push_diff` applies the diff the agent produced in the sandbox, commits it on
  a work branch and pushes that branch (never the base) to the remote.

Plain ``subprocess git`` — no new dependencies.

Token handling (the security-critical part). A Gitea access token is passed to git
*only* through the process environment, as an HTTP request header::

    GIT_CONFIG_COUNT=1
    GIT_CONFIG_KEY_0=http.extraHeader
    GIT_CONFIG_VALUE_0=Authorization: token <token>

``GIT_CONFIG_*`` (git >= 2.31) injects git config without touching argv or the
on-disk ``.git/config``. This was chosen over embedding credentials in the push URL
(``https://oauth2:<token>@host/...``) because a URL would leak the token into the
process command line (visible in ``ps``), into ``CalledProcessError.cmd`` and into any
logged remote URL, and would persist it in ``.git/config`` via ``remote set-url``.
The header keeps the token out of every one of those, and ``Authorization: token ...``
is Gitea's documented scheme for git-over-HTTP with no username required. When no token
is given git pushes to the remote exactly as configured (local-path remotes in tests),
and the header injection is skipped entirely.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

# git commit identity (author and committer) — deterministic, not the operator's.
AGENT_RUNTIME_NAME = "agent-runtime"
AGENT_RUNTIME_EMAIL = "agent-runtime@localhost"

# Retries onto the same work branch may diverge (fresh base + a new commit).
# --force-with-lease lets the worker overwrite its own branch while still refusing to
# clobber a branch someone else moved in between.
_PUSH_REFSPEC_FMT = "HEAD:refs/heads/{branch}"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GitOperationError(RuntimeError):
    """A git subprocess failed (non-zero exit) or its output was unusable."""


def _git_env(token: str | None = None) -> dict[str, str]:
    """Build the git environment: pinned identity plus, when given, a token header.

    The token is placed only in the ``GIT_CONFIG_*`` environment (see module docstring)
    so it never reaches argv or on-disk git state.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": AGENT_RUNTIME_NAME,
            "GIT_AUTHOR_EMAIL": AGENT_RUNTIME_EMAIL,
            "GIT_COMMITTER_NAME": AGENT_RUNTIME_NAME,
            "GIT_COMMITTER_EMAIL": AGENT_RUNTIME_EMAIL,
            # never block on an interactive credential prompt in a sandbox
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if token:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: token {token}"
    return env


def _repo_dir_name(repo_url: str) -> str:
    """Derive a stable local directory name from a remote URL or path."""
    tail = repo_url.rstrip("/").rsplit("/", 1)[-1]
    # scp-style remotes (git@host:owner/repo.git) use a ':' instead of the last '/'
    tail = tail.rsplit(":", 1)[-1]
    name = tail[: -len(".git")] if tail.endswith(".git") else tail
    return name or "repo"


def _run(
    args: Sequence[str],
    cwd: Path,
    env: dict[str, str],
    *,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` and wrap any failure in :class:`GitOperationError`.

    The message quotes argv (which never carries the token — credentials travel in
    ``env``, see :func:`_git_env`) and git's stderr; git does not echo an
    ``http.extraHeader`` value, so no credential leaks through the detail either.
    """
    argv = ["git", *args]
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise GitOperationError(f"`{exc.cmd!r}` failed (exit {exc.returncode}): {detail}") from exc


def _clean_worktree(repo_dir: Path, env: dict[str, str]) -> bool:
    """True when the working tree has no staged or unstaged tracked changes."""
    result = _run(["status", "--porcelain", "--untracked-files=no"], repo_dir, env)
    return not result.stdout.strip()


def ensure_repo(
    workspace: Path,
    repo_url: str,
    base_branch: str,
    *,
    token: str | None = None,
) -> Path:
    """Clone the repo into ``workspace`` or reset an existing clone to ``origin/<base>``.

    First call runs ``git clone --branch <base>``; later calls fetch the base branch and
    hard-reset onto ``origin/<base>``, discarding any local commit or working-tree edit
    from a previous vertex so every run starts from a pristine base. The remote is kept
    as configured (no credentials embedded). Returns the clone directory.
    """
    env = _git_env(token)
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = workspace / _repo_dir_name(repo_url)

    if (repo_dir / ".git").is_dir():
        _run(["fetch", "origin", base_branch], repo_dir, env)
        _run(["checkout", "-f", "-B", base_branch, f"origin/{base_branch}"], repo_dir, env)
        _run(["reset", "--hard", f"origin/{base_branch}"], repo_dir, env)
        _run(["clean", "-fdx"], repo_dir, env)
    else:
        _run(["clone", "--branch", base_branch, repo_url, str(repo_dir)], workspace, env)
    return repo_dir


def push_diff(
    repo_dir: Path,
    diff: str,
    work_branch: str,
    *,
    message: str,
    token: str | None = None,
) -> str:
    """Apply ``diff`` on a fresh work branch, commit it, push the branch; return the sha.

    Fails loudly (raises :class:`GitOperationError`) unless the base clone is clean, the
    diff is non-empty, and every hunk applies — with no side effects in those cases (no
    commit, no branch move, no partial working-tree edit). ``diff`` is the unified diff
    the sandbox produced (joined per-file ``git diff`` patches); deletions and new files
    are carried by ``git apply --index``.
    """
    env = _git_env(token)

    if not diff.strip():
        raise GitOperationError("refusing to push an empty diff: nothing to commit")
    if not _clean_worktree(repo_dir, env):
        raise GitOperationError(
            f"working tree at {repo_dir} is not clean; refusing to apply a diff onto local changes"
        )

    base_sha = _run(["rev-parse", "HEAD"], repo_dir, env).stdout.strip()

    # Apply straight into the index FIRST: git apply is atomic, so a conflicting or
    # malformed patch aborts here with no branch created and no partial write. Only once
    # every hunk lands do we move onto the work branch (staged changes carry over) and
    # commit — this order keeps a failed push_diff free of side effects on the base clone.
    _run(["apply", "--index", "--whitespace=nowarn", "-"], repo_dir, env, stdin=diff)
    _run(["checkout", "-B", work_branch], repo_dir, env)
    _run(["-c", "commit.gpgsign=false", "commit", "-m", message], repo_dir, env)

    sha = _run(["rev-parse", "HEAD"], repo_dir, env).stdout.strip()
    if not _SHA_RE.match(sha):
        raise GitOperationError(f"git returned an unusable commit sha: {sha!r}")
    if sha == base_sha:
        raise GitOperationError("commit sha is unchanged from the base; the diff applied nothing")

    refspec = _PUSH_REFSPEC_FMT.format(branch=work_branch)
    _run(["push", "--force-with-lease", "origin", refspec], repo_dir, env)
    return sha
