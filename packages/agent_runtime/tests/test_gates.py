"""Gate driver: PR → CI → review → fix rounds → HITL/auto-merge → merge (M5 Task 6)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agent_runtime.gates import FixRequest, FixResult, GateConfig, GateOutcome, run_gates
from agent_runtime.gitea_prs import PullRequest, ReviewComment
from agent_runtime.reviewer import Finding, Review, ReviewUnavailable

DOCS_POLICY = 'auto_merge:\n  enabled: true\n  paths: ["docs/**", "*.md"]\n'


class FakePR:
    """In-memory PR whose CI state sequence and files the test controls."""

    def __init__(self, ci: list[str], files: list[str] | None = None) -> None:
        self.ci = list(ci)  # consumed one per poll; last value sticks
        self.files = files or ["src/x.py"]
        self.merged = False
        self.closed = False
        self.merge_confirms = True
        self.reviews: list[tuple[str, str, list[ReviewComment]]] = []
        self.comments: list[str] = []
        self.ensure_calls = 0

    def _pr(self) -> PullRequest:
        return PullRequest(
            number=3,
            url="http://g/o/r/pulls/3",
            head_sha="sha",
            state="closed" if self.closed else "open",
            merged=self.merged and self.merge_confirms,
        )

    async def ensure_pr(self, owner: str, repo: str, **_: Any) -> PullRequest:
        self.ensure_calls += 1
        return self._pr()

    async def get_pr(self, owner: str, repo: str, number: int) -> PullRequest:
        return self._pr()

    async def combined_status(self, owner: str, repo: str, sha: str) -> str:
        return self.ci.pop(0) if len(self.ci) > 1 else self.ci[0]

    async def changed_files(self, owner: str, repo: str, number: int) -> list[str]:
        return list(self.files)

    async def pr_diff(self, owner: str, repo: str, number: int) -> str:
        return "diff --git a/src/x.py b/src/x.py\n+x\n"

    async def post_review(
        self, owner: str, repo: str, number: int, *, body: str, comments: Any, event: str
    ) -> None:
        self.reviews.append((event, body, list(comments)))

    async def comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self.comments.append(body)

    async def merge(self, owner: str, repo: str, number: int, *, message: str) -> None:
        self.merged = True

    async def close(self, owner: str, repo: str, number: int) -> None:
        self.closed = True


class FakeHITL:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.created: list[tuple[str, str, dict[str, Any]]] = []

    async def create(self, kind: str, subject: str, context: dict[str, Any]) -> int:
        self.created.append((kind, subject, context))
        return 11

    async def status(self, request_id: int) -> str:
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def clean_review() -> Review:
    return Review(summary="ok", findings=[], model="reviewer", prompt_version="v1", truncated=False)


def blocking_review() -> Review:
    return Review(
        summary="bug",
        findings=[Finding(path="src/x.py", line=1, severity="blocking", body="off by one")],
        model="reviewer",
        prompt_version="v1",
        truncated=False,
    )


async def instant(_: float) -> None:
    await asyncio.sleep(0)


async def _run(
    pr: FakePR,
    *,
    reviews: list[Any] | None = None,
    hitl: FakeHITL | None = None,
    fix_result: FixResult | None = None,
    on_fix: Any = None,
    policy: str | None = None,
    cfg: GateConfig | None = None,
    stop: asyncio.Event | None = None,
) -> tuple[GateOutcome, list[str], list[FixRequest], FakeHITL]:
    queue = list(reviews or [clean_review()])
    notes: list[str] = []
    fixes: list[FixRequest] = []
    hitl = hitl or FakeHITL(["approved"])

    async def review(goal: str, diff: str) -> Review:
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, Review)
        return item

    async def fix(req: FixRequest) -> FixResult:
        fixes.append(req)
        if on_fix is not None:
            on_fix()
        return fix_result or FixResult(pushed=True, replies={"src/x.py:1": "fixed"})

    async def notify(text: str) -> None:
        notes.append(text)

    outcome = await run_gates(
        owner="o",
        repo="r",
        branch="agent/vertex-5",
        base="main",
        title="t",
        body="b",
        goal="g",
        policy_text=policy,
        pr=pr,
        review=review,
        fix=fix,
        hitl=hitl,
        notify=notify,
        cfg=cfg or GateConfig(ci_timeout_s=5, ci_poll_s=1, hitl_poll_s=1),
        stop=stop or asyncio.Event(),
        sleep=instant,
    )
    return outcome, notes, fixes, hitl


async def test_happy_path_hitl_approved_merges() -> None:
    pr = FakePR(["pending", "success"])
    outcome, notes, fixes, hitl = await _run(pr)
    assert outcome.status == "merged" and pr.merged
    assert outcome.pr_url == "http://g/o/r/pulls/3"
    assert [c[0] for c in hitl.created] == ["merge"]
    assert fixes == []
    assert any("PR opened" in n for n in notes) and any("merged" in n for n in notes)
    assert pr.reviews[0][0] == "COMMENT"


async def test_docs_only_auto_merges_without_hitl() -> None:
    pr = FakePR(["success"], files=["docs/a.md"])
    outcome, _, _, hitl = await _run(pr, policy=DOCS_POLICY)
    assert outcome.status == "merged" and outcome.auto_merged
    assert hitl.created == []


async def test_platform_repo_always_hitl() -> None:
    pr = FakePR(["success"], files=["docs/a.md"])
    cfg = GateConfig(ci_timeout_s=5, ci_poll_s=1, hitl_poll_s=1, platform_repo=True)
    outcome, _, _, hitl = await _run(pr, policy=DOCS_POLICY, cfg=cfg)
    assert outcome.status == "merged" and not outcome.auto_merged
    assert len(hitl.created) == 1


async def test_red_ci_triggers_fix_round_then_merges() -> None:
    pr = FakePR(["failure"])

    def turn_green() -> None:
        pr.ci = ["pending", "success"]

    outcome, notes, fixes, _ = await _run(pr, on_fix=turn_green)
    assert outcome.status == "merged" and outcome.rounds == 1
    assert len(fixes) == 1 and fixes[0].round == 1
    assert any("fix round 1" in n for n in notes)


async def test_blocking_review_triggers_fix_and_replies_posted() -> None:
    pr = FakePR(["success"])
    outcome, _, fixes, _ = await _run(pr, reviews=[blocking_review(), clean_review()])
    assert outcome.status == "merged"
    assert [f.body for f in fixes[0].blocking] == ["off by one"]
    assert pr.reviews[0][0] == "REQUEST_CHANGES"
    assert any("fixed" in c for c in pr.comments)


async def test_fix_rounds_exhausted_blocks() -> None:
    pr = FakePR(["failure"])
    outcome, _, fixes, _ = await _run(pr)
    assert outcome.status == "blocked" and outcome.reason == "fix_rounds_exhausted"
    assert len(fixes) == 2 and not pr.merged


async def test_fix_not_pushed_blocks_with_reason() -> None:
    pr = FakePR(["failure"])
    outcome, _, _, _ = await _run(pr, fix_result=FixResult(pushed=False, replies={}, reason="x"))
    assert outcome.status == "blocked" and "x" in outcome.reason and not pr.merged


async def test_ci_timeout_blocks_without_merge() -> None:
    pr = FakePR(["pending"])
    outcome, _, _, _ = await _run(pr)
    assert outcome.status == "blocked" and outcome.reason == "ci_timeout"
    assert not pr.merged


@pytest.mark.parametrize("state", ["error"])
async def test_ci_error_counts_as_red(state: str) -> None:
    pr = FakePR([state])
    outcome, _, fixes, _ = await _run(pr)
    assert outcome.status == "blocked" and fixes and not pr.merged


async def test_review_unavailable_blocks() -> None:
    pr = FakePR(["success"])
    outcome, _, _, _ = await _run(pr, reviews=[ReviewUnavailable("down")])
    assert outcome.status == "blocked" and outcome.reason == "review_unavailable"
    assert not pr.merged


async def test_protected_path_blocks_before_review() -> None:
    pr = FakePR(["success"], files=["src/x.py", ".gitea/workflows/ci.yml"])
    outcome, _, _, _ = await _run(pr)
    assert outcome.status == "blocked" and outcome.reason.startswith("protected_path")
    assert pr.reviews == [] and not pr.merged


async def test_hitl_rejected_closes_pr() -> None:
    pr = FakePR(["success"])
    outcome, _, _, _ = await _run(pr, hitl=FakeHITL(["pending", "rejected"]))
    assert outcome.status == "blocked" and outcome.reason == "hitl_rejected"
    assert pr.closed and not pr.merged


async def test_stop_while_waiting_hitl_leaves_pr_open() -> None:
    pr = FakePR(["success"])
    stop = asyncio.Event()
    hitl = FakeHITL(["pending"])

    async def stopper() -> None:
        while not hitl.created:
            await asyncio.sleep(0)
        stop.set()

    task = asyncio.create_task(stopper())
    outcome, _, _, _ = await _run(pr, hitl=hitl, stop=stop)
    await task
    assert outcome.status == "stopped"
    assert not pr.closed and not pr.merged


async def test_merge_unconfirmed_blocks() -> None:
    pr = FakePR(["success"])
    pr.merge_confirms = False
    outcome, _, _, _ = await _run(pr)
    assert outcome.status == "blocked" and outcome.reason == "merge_unconfirmed"
