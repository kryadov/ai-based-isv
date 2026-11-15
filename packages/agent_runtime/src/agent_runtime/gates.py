"""Gate corridor driver: PR → CI → bot review → fix rounds → HITL/auto-merge → merge.

Spec §7.2 item 4 (M5). Runs in the host worker after a vertex pushed its
branch. All I/O goes through narrow ports (Gitea PR client, reviewer, fix
attempt, HITL, tracker notify), so the ordering rules below are unit-tested:

* fail closed — an unknown/red CI, a reviewer failure, a protected gate path,
  a timeout or a rejected HITL never merges;
* the PR is reused across attempts (``ensure_pr``);
* a stop request leaves the PR open for the next attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from agent_runtime.gitea_prs import PullRequest, ReviewComment
from agent_runtime.merge_policy import auto_merge_allowed, load_policy, touches_protected
from agent_runtime.reviewer import Finding, Review, ReviewUnavailable

logger = logging.getLogger(__name__)

GateStatus = Literal["merged", "blocked", "stopped"]


@dataclass(frozen=True)
class GateConfig:
    max_fix_rounds: int = 2
    ci_timeout_s: float = 1200.0
    ci_poll_s: float = 10.0
    hitl_poll_s: float = 15.0
    platform_repo: bool = False


@dataclass(frozen=True)
class GateOutcome:
    status: GateStatus
    pr_url: str
    reason: str = ""
    rounds: int = 0
    review_summary: str = ""
    auto_merged: bool = False


@dataclass(frozen=True)
class FixRequest:
    round: int
    ci_state: str
    ci_log_tail: str
    blocking: list[Finding] = field(default_factory=list)


@dataclass(frozen=True)
class FixResult:
    pushed: bool
    replies: dict[str, str]
    reason: str = ""


class PRPort(Protocol):
    async def ensure_pr(
        self, owner: str, repo: str, *, head: str, base: str, title: str, body: str
    ) -> PullRequest: ...
    async def get_pr(self, owner: str, repo: str, number: int) -> PullRequest: ...
    async def combined_status(self, owner: str, repo: str, sha: str) -> Any: ...
    async def changed_files(self, owner: str, repo: str, number: int) -> list[str]: ...
    async def pr_diff(self, owner: str, repo: str, number: int) -> str: ...
    async def post_review(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        body: str,
        comments: list[ReviewComment],
        event: Any,
    ) -> None: ...
    async def comment(self, owner: str, repo: str, number: int, body: str) -> None: ...
    async def merge(self, owner: str, repo: str, number: int, *, message: str) -> None: ...
    async def close(self, owner: str, repo: str, number: int) -> None: ...


class HITLPort(Protocol):
    async def create(self, kind: str, subject: str, context: dict[str, Any]) -> int: ...
    async def status(self, request_id: int) -> str: ...


ReviewPort = Callable[[str, str], Awaitable[Review]]
FixPort = Callable[[FixRequest], Awaitable[FixResult]]
NotifyPort = Callable[[str], Awaitable[None]]
CILogPort = Callable[[int], Awaitable[str]]
Sleep = Callable[[float], Awaitable[None]]


class _Stopped(Exception):
    pass


async def _no_log(_: int) -> str:
    return ""


async def run_gates(
    *,
    owner: str,
    repo: str,
    branch: str,
    base: str,
    title: str,
    body: str,
    goal: str,
    policy_text: str | None,
    pr: PRPort,
    review: ReviewPort,
    fix: FixPort,
    hitl: HITLPort,
    notify: NotifyPort,
    cfg: GateConfig,
    stop: asyncio.Event,
    sleep: Sleep = asyncio.sleep,
    ci_log: CILogPort = _no_log,
) -> GateOutcome:
    async def say(text: str) -> None:
        with contextlib.suppress(Exception):
            await notify(text)

    opened = await pr.ensure_pr(owner, repo, head=branch, base=base, title=title, body=body)
    number, url = opened.number, opened.url
    await say(f"PR opened: {url}")

    def blocked(reason: str, rounds: int, summary: str = "") -> GateOutcome:
        logger.info("gates blocked %s: %s", url, reason)
        return GateOutcome("blocked", url, reason=reason, rounds=rounds, review_summary=summary)

    summary = ""
    rounds = 0
    try:
        for round_no in range(cfg.max_fix_rounds + 1):
            rounds = round_no
            current = await pr.get_pr(owner, repo, number)
            files = await pr.changed_files(owner, repo, number)
            protected = touches_protected(files)
            if protected:
                return blocked(f"protected_path: {', '.join(protected)}", rounds)

            ci_state = await _wait_ci(pr, owner, repo, current.head_sha, cfg, stop, sleep)
            if ci_state is None:
                return blocked("ci_timeout", rounds)
            await say(f"CI: {ci_state}")

            try:
                result = await review(goal, await pr.pr_diff(owner, repo, number))
            except ReviewUnavailable:
                return blocked("review_unavailable", rounds)
            summary = result.summary
            await pr.post_review(
                owner,
                repo,
                number,
                body=result.summary,
                comments=[
                    ReviewComment(path=f.path, line=f.line, body=f"[{f.severity}] {f.body}")
                    for f in result.findings
                ],
                event="REQUEST_CHANGES" if result.blocking else "COMMENT",
            )
            await say(f"review: {result.summary} ({len(result.blocking)} blocking)")

            if ci_state == "success" and not result.blocking:
                break
            if round_no == cfg.max_fix_rounds:
                return blocked("fix_rounds_exhausted", rounds, summary)

            log_tail = "" if ci_state == "success" else await ci_log(number)
            outcome = await fix(
                FixRequest(
                    round=round_no + 1,
                    ci_state=ci_state,
                    ci_log_tail=log_tail,
                    blocking=result.blocking,
                )
            )
            if not outcome.pushed:
                return blocked(f"fix_failed: {outcome.reason}", rounds + 1, summary)
            for thread, reply in outcome.replies.items():
                await pr.comment(owner, repo, number, f"`{thread}`: {reply}")
            await say(f"fix round {round_no + 1}")

        files = await pr.changed_files(owner, repo, number)
        policy = load_policy(policy_text, platform_repo=cfg.platform_repo)
        auto = auto_merge_allowed(files, policy)
        if not auto:
            request_id = await hitl.create(
                "merge",
                url,
                {"pr_url": url, "review_summary": summary, "rounds": rounds, "ci": "success"},
            )
            await say(f"waiting for human merge approval (HITL #{request_id})")
            decision = await _wait_hitl(hitl, request_id, cfg, stop, sleep)
            if decision == "rejected":
                await pr.close(owner, repo, number)
                return blocked("hitl_rejected", rounds, summary)

        await pr.merge(owner, repo, number, message=f"{title}\n\n{url}")
        if not (await pr.get_pr(owner, repo, number)).merged:
            return blocked("merge_unconfirmed", rounds, summary)
    except _Stopped:
        return GateOutcome("stopped", url, reason="stopped", rounds=rounds, review_summary=summary)

    await say(f"merged: {url}")
    return GateOutcome("merged", url, rounds=rounds, review_summary=summary, auto_merged=auto)


async def _wait_ci(
    pr: PRPort,
    owner: str,
    repo: str,
    sha: str,
    cfg: GateConfig,
    stop: asyncio.Event,
    sleep: Sleep,
) -> str | None:
    """Final CI state (success/failure/error), or None on timeout."""
    waited = 0.0
    while True:
        if stop.is_set():
            raise _Stopped
        state = str(await pr.combined_status(owner, repo, sha))
        if state in {"success", "failure", "error"}:
            return state
        if waited >= cfg.ci_timeout_s:
            return None
        await sleep(cfg.ci_poll_s)
        waited += cfg.ci_poll_s


async def _wait_hitl(
    hitl: HITLPort, request_id: int, cfg: GateConfig, stop: asyncio.Event, sleep: Sleep
) -> str:
    """``approved`` or ``rejected``; raises _Stopped on a stop request."""
    while True:
        status = await hitl.status(request_id)
        if status in {"approved", "rejected"}:
            return status
        if stop.is_set():
            raise _Stopped
        await sleep(cfg.hitl_poll_s)
