"""Worker side of the M5 gate corridor: wiring ``gates.run_gates`` to real ports.

``_process_vertex`` (worker.py) calls a :data:`GateRunner` after it pushed the
vertex branch. The default runner talks to Gitea (PR client), the LiteLLM
reviewer, the agent_api HITL inbox and the tracker; tests inject their own.
A fix attempt re-runs the executor on the PR branch with the CI failure and
the blocking review findings, applies its diff and pushes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from agent_runtime.executor import ExecutionRequest, ExecutionStatus, Executor
from agent_runtime.gates import (
    FixPort,
    FixRequest,
    FixResult,
    GateConfig,
    GateOutcome,
    NotifyPort,
    run_gates,
)
from agent_runtime.gitea_prs import GiteaPRClient
from agent_runtime.gitops import apply_diff, push_head
from agent_runtime.hitl_client import HITLClient
from agent_runtime.merge_policy import touches_protected
from agent_runtime.pathguard import DiffUnparseable, diff_files, filter_violations
from agent_runtime.reviewer import Review, review_diff
from agent_runtime.settings import WorkerSettings

POLICY_PATH = ".platform/merge-policy.yaml"
_LOG_TAIL_LINES = 200


@dataclass(frozen=True)
class GateContext:
    vertex: dict[str, Any]
    branch: str
    title: str
    body: str
    goal: str
    policy_text: str | None
    fix: FixPort
    notify: NotifyPort


GateRunner = Callable[[GateContext, asyncio.Event], Awaitable[GateOutcome]]


def repo_coords(repo_url: str) -> tuple[str, str, str]:
    """``http://host:port/owner/repo(.git)`` → (base_url, owner, repo)."""
    parts = urlsplit(repo_url)
    segments = [s for s in parts.path.split("/") if s]
    if not parts.scheme or len(segments) < 2:
        raise ValueError(f"cannot derive Gitea owner/repo from {repo_url!r}")
    repo = segments[-1].removesuffix(".git")
    return f"{parts.scheme}://{parts.netloc}", segments[-2], repo


def read_policy(repo_dir: Path, base_branch: str) -> str | None:
    """The merge policy as committed on the BASE branch — never the agent's copy."""
    proc = subprocess.run(  # noqa: S603 - fixed git argv
        ["git", "show", f"origin/{base_branch}:{POLICY_PATH}"],  # noqa: S607
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def build_fix_prompt(vertex: dict[str, Any], req: FixRequest) -> str:
    """The fix-attempt prompt: original task, CI failure tail, blocking findings."""
    findings = "\n".join(f"- {f.path}:{f.line} — {f.body}" for f in req.blocking) or "(none)"
    log = "\n".join(req.ci_log_tail.splitlines()[-_LOG_TAIL_LINES:]) or "(no log)"
    return (
        f"Fix round {req.round} for task: {vertex.get('name') or 'unnamed'}\n"
        f"Allowed paths: {', '.join(vertex.get('paths_allow', [])) or '(none)'}\n"
        f"Done when: {vertex.get('done_when', '')}\n\n"
        f"The pull request for this task is not mergeable yet. CI state: {req.ci_state}.\n\n"
        f"Blocking review findings to fix:\n{findings}\n\n"
        f"CI log tail:\n{log}\n\n"
        "Fix the code so CI passes and every blocking finding is resolved. "
        "Do not touch .gitea/ or .platform/ (gate files). Do not push or commit; "
        "do not run commands. Write the files, then stop."
    )


async def fix_attempt(
    *,
    settings: WorkerSettings,
    executor: Executor,
    vertex: dict[str, Any],
    req: FixRequest,
    repo_dir: Path,
    branch: str,
    token: str | None,
) -> FixResult:
    """Run one fix session on ``branch`` and push its diff; never raises."""
    request = ExecutionRequest(
        run_id=settings.run_id,
        vertex_id=int(vertex["id"]),
        name=f"{vertex.get('name', '')} (fix {req.round})",
        prompt=build_fix_prompt(vertex, req),
        paths_allow=list(vertex.get("paths_allow", [])),
        done_when=vertex.get("done_when", ""),
        repo_url=settings.repo_url,
        base_branch=branch,  # the sandbox clones the PR branch, not main
        work_branch=branch,
    )
    session = None
    try:
        session = await executor.start(request)
        try:
            status = await asyncio.wait_for(
                session.wait_done(), timeout=settings.session_timeout_seconds
            )
        except TimeoutError:
            return FixResult(pushed=False, replies={}, reason="fix_session_timeout")
        if status != ExecutionStatus.DONE:
            return FixResult(pushed=False, replies={}, reason=f"fix_session:{status.value}")
        diff = await session.diff()
        try:
            changed = diff_files(diff)
        except DiffUnparseable as exc:
            return FixResult(pushed=False, replies={}, reason=f"fix_diff_unparseable: {exc}")
        bad = filter_violations(changed, request.paths_allow) + touches_protected(changed)
        if bad:
            return FixResult(pushed=False, replies={}, reason=f"fix_paths_violation: {bad}")
        await asyncio.to_thread(apply_diff, repo_dir, diff, token=token)
        await asyncio.to_thread(
            push_head, repo_dir, branch, message=f"agent: fix round {req.round}", token=token
        )
    except Exception as exc:
        return FixResult(pushed=False, replies={}, reason=f"fix_failed: {type(exc).__name__}")
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                await session.stop()
    replies = {f"{f.path}:{f.line}": f"addressed in fix round {req.round}" for f in req.blocking}
    return FixResult(pushed=True, replies=replies)


def reviewer_base(settings: WorkerSettings) -> str:
    """LiteLLM base URL (without ``/v1``) the host-side reviewer calls."""
    url = settings.reviewer_base_url or settings.litellm_base_url
    return url.rstrip("/").removesuffix("/v1")


def default_gate_runner(settings: WorkerSettings, token: str) -> GateRunner:
    """The production runner: Gitea + LiteLLM reviewer + agent_api HITL."""
    base_url, owner, repo = repo_coords(settings.repo_url)
    if settings.gitea_url:  # the API endpoint may differ from the clone URL
        base_url = settings.gitea_url.rstrip("/")
    cfg = GateConfig(
        max_fix_rounds=settings.max_fix_rounds,
        ci_timeout_s=settings.ci_timeout_s,
        hitl_poll_s=settings.hitl_poll_s,
        platform_repo=settings.platform_repo,
    )

    async def runner(ctx: GateContext, stop: asyncio.Event) -> GateOutcome:
        async with httpx.AsyncClient(timeout=60.0) as http:
            pr = GiteaPRClient(http, base_url=base_url, token=token)
            hitl = HITLClient(http, settings.api_url)

            async def review(goal: str, diff: str) -> Review:
                return await review_diff(
                    http,
                    base_url=reviewer_base(settings),
                    api_key=settings.litellm_api_key,
                    model=settings.reviewer_model,
                    goal=goal,
                    diff=diff,
                )

            async def ci_log(number: int) -> str:
                return await pr.job_log_tail(owner, repo, number, tail_lines=_LOG_TAIL_LINES)

            return await run_gates(
                owner=owner,
                repo=repo,
                branch=ctx.branch,
                base=settings.base_branch,
                title=ctx.title,
                body=ctx.body,
                goal=ctx.goal,
                policy_text=ctx.policy_text,
                pr=pr,
                review=review,
                fix=ctx.fix,
                hitl=hitl,
                notify=ctx.notify,
                cfg=cfg,
                stop=stop,
                ci_log=ci_log,
            )

    return runner
