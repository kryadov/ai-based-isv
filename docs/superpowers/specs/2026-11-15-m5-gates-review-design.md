# M5 — Gates and review corridor (design)

Status: design, 2026-11-15. Spec authority: `2026-11-07-swarm-dev-platform-design.md` §7.2 item 4 (gates), §8 M5, §13.7 (recursive self-development). Builds on M4 (`2026-11-09-m4-agent-runtime-design.md`, release v0.4.0).

## Intent and why

M4 ends at a pushed branch `agent/vertex-N` plus a tracker comment. No PR is opened, no CI runs on the target repo, nothing reviews the change and nothing merges it. The owner's goal (2026-11-15) is to "get the platform on rails ASAP and start development": the path to spec §13.7 level 2 is the shortest, where this repo is seeded into Gitea and the platform implements its own increments. That needs the **delivery corridor from a branch to a merged, reviewed change** first. Canary rollout is M6; memory is M7.

So M5 delivers one end-to-end corridor for the agent's change:

**vertex done → PR opened → CI (stack check profile + security scans) → bot review → agent fixes or justifies (bounded rounds) → HITL merge gate (or policy auto-merge) → merge → tracker Done with the PR link.**

## Goals

1. **The PR is the unit of delivery.** The worker opens a Gitea PR from `agent/vertex-N` to the default branch. The body carries the evidence summary: the verdict, the diff stat, the done_when results and the manifest path.
2. **CI corridor in the target repo**, as a Gitea Actions workflow seeded by the bootstrapper from a **per-stack check profile**. v0 ships the `python` profile (ruff check + format, mypy, pytest) plus stack-agnostic security: **gitleaks** (secret scan) and **semgrep** (SAST, `p/ci` ruleset). Branch protection on the default branch requires the CI status check. This is "CI must be 100% green", with no skip switches.
3. **MCP `ci` server**: read-only tools for agents and the worker, `get_pr_checks(pr_ref)` and `get_job_log(pr_ref, job)` (truncated tail), so a fixing agent sees why CI failed.
4. **Bot review, first pass**: an LLM reviewer reads the PR diff and the vertex goal, and posts a Gitea PR review with line comments, each tagged `blocking` or `nit`. The reviewer uses a **different model family from the implementer** (research 2026-11-14: cross-family review). It goes through a new LiteLLM alias `reviewer`, with the role prompt versioned in the repo.
5. **Fix loop**: on red CI or blocking review comments, the worker runs a **fix attempt**: a new agent session on the same branch, with the CI failure tail and the open blocking threads as its input. The agent must either fix each thread, or answer it with a justification (it posts a reply), and must make CI green. The loop is bounded by `max_fix_rounds` (default 2). After that, the vertex is **Blocked** and a HITL `escalation` is filed. Disputes go to a human, per the spec.
6. **HITL gate №2 (merge)**: with CI green and no open blocking threads, the worker files a HITL `merge` request in agent_api, with the PR link and an evidence summary, and waits. Approve → merge via the Gitea API (merge commit). Reject → close the PR and mark the vertex Blocked with the reason.
7. **Auto-merge policy**: a repo-level policy file (`.platform/merge-policy.yaml`, seeded) lists low-risk path classes, v0 `docs/**`, `*.md`. A PR whose every changed path matches is merged without HITL once CI is green and review is clean. **Default: everything else needs HITL.** For this platform's own repo (§13.7), auto-merge is disabled entirely (spec: "HITL on every merge into the platform's main").
8. **Tracker stage callbacks** (TODO feedback loop): PR opened (link), CI passed or failed, review posted, fix round N, merged (link), all as `[platform]` comments through the existing task-source. Done is set only after the merge. Blocked carries a reason.
9. **Acceptance flow `infra/tests/m5_flow.sh`**: issue → agent → PR → CI green → bot review posted → HITL merge request pending → approve through the API → PR merged → issue closed with the PR link. A second scenario plants a failing test to prove the fix loop: CI goes red, the fix round makes it green. Regressions m2a–m4c stay green.

## Non-goals (M5)

- Canary/rollout and metric analysis (M6). Org memory (M7). Multi-vertex swarm scheduling and the merge queue at scale (M8; the existing `merge_queue` table stays as is).
- Stack profiles beyond `python`: Java/TS profiles come later as data (the profile mechanism is in M5, more profiles are not).
- HITL gates 1 (plan approval) and 3 (canary promotion): the HITL kinds exist, the wiring belongs to the stages that need them.
- A UI: approval goes through the agent_api HTTP endpoint or `org-bootstrap`/curl. The panel is M10.
- DB-migration vertex class (spec §7.2): later, with the first target project that has a DB.

## Anti-goals (failure even if it "works")

- Any way for an agent to bypass a gate: skip markers, editing the workflow, editing the merge policy or branch protection from inside a vertex. `paths_allow` must deny `.gitea/**` and `.platform/**` to implementer vertices, and the worker re-checks the PR diff for those paths before every merge.
- Silent degradation: a failed reviewer call or an unreachable CI must block the merge, never pass it.
- The same model family both implementing and reviewing, as the only review.
- Secrets in PR bodies, comments or CI logs posted back into the tracker.

## Constraints

- Gitea 1.2x Actions with the existing `act_runner` (dind, label `ubuntu-latest` → `gitea/runner-images:ubuntu-latest`). CI jobs must work offline from GitHub where possible: pin tool versions and install via `uv`/pipx inside the job.
- The LLM is external (z.ai Coding Plan, OpenRouter fallback): reviewer calls cost money. One reviewer call per round, with the diff truncated to a budget (default 60k chars; above that, review the file list and the largest hunks and say so in the review).
- Memory: one stand of 16 GB. CI jobs run on the single dind runner sequentially.
- Platform code standards: mypy --strict, ruff, TDD; the acceptance flow runs through the toolbox.

## Approaches considered

1. **Gate pipeline in the host worker (chosen).** After a vertex succeeds, the run-scoped worker drives the corridor: open PR → poll CI → review → fix rounds → HITL → merge. This reuses the lease/heartbeat, the evidence and the tracker loop already in the worker, and it is the smallest step to an end-to-end corridor. Cost: the worker holds the lease while waiting on CI or a human. Mitigation: the lease is heartbeated anyway; the HITL wait is polled with a long interval, and the worker exits cleanly on stop.
2. **Webhook-driven gate controller service** (Gitea webhooks → agent_api state machine). Scales and survives worker restarts, but means a new service, webhook delivery and idempotency in v0. Revisit at M8 (swarm), when many PRs are in flight.
3. **Everything as CI jobs**, with the LLM review as a workflow step. Couples LLM keys to the CI runner, and the fix loop and HITL do not fit CI semantics.

Chosen: **1**, with the corridor as a separate module (`agent_runtime/gates.py`) behind narrow seams (Gitea PR client, CI status client, reviewer, HITL client). Approach 2 can later replace the driver without touching the gate logic.

## Design decisions (builder's calls, made by the implementer)

- **Gitea PR/CI client**: extend `mcp_servers/gitea_client.py` (or a sibling in agent_runtime) with `create_pr`, `get_pr`, `list_commit_statuses`, `post_review`, `list_review_comments`, `reply`, `merge_pr` and `close_pr`, against fakes in tests. The token is from OpenBao as today.
- **CI status source**: Gitea commit statuses for the PR head SHA (Actions reports them). States: success / failure / error / pending. Timeout (default 20 min) → Blocked, "CI did not report".
- **Profile delivery**: `infra/templates/check-profiles/python/` holds `.gitea/workflows/ci.yml` and `.platform/merge-policy.yaml`. The bootstrapper seeds them into new target repos (`seed-demo` / project onboarding), and the acceptance flow seeds them into `target-scratch`. Branch protection gets `enable_status_check` with the CI context name.
- **Reviewer**: a PydanticAI-free plain LiteLLM chat call with structured JSON output `{summary, comments: [{path, line, severity: blocking|nit, body}]}`, validated by pydantic. Invalid JSON gets one retry, then the merge is blocked. LiteLLM alias `reviewer` → OpenRouter `moonshotai/kimi-k3.1-pro` (cross-family vs the GLM implementer), with fallback `z-ai/glm-5.3` and a review note that the fallback was same-family. The prompt lives at `packages/agent_runtime/src/agent_runtime/prompts/reviewer-v1.md`, and its version is recorded in the run manifest.
- **Fix attempt**: reuses the executor (same sandbox backend) with a prompt built from the CI log tail and the blocking comments. Its diff is applied to the PR branch the same way (paths guard, done_when re-run). Replies to threads are posted by the worker from the agent's structured final message (`{thread_id: reply}`), not by the agent holding a write token. Agents never get write tools (keeps review I3).
- **HITL**: `POST /hitl` with kind `merge` and context `{pr_url, verdict, ci, review_summary, rounds}`. The worker polls `GET /hitl/{id}` (endpoint added) at a configurable interval. Approve and reject are audited by agent_api, as they are today.
- **Merge**: Gitea merge API, style `merge`. Afterwards the worker re-reads the PR to confirm it is merged, then posts the tracker Done comment with the PR link.
- **Config**: `WorkerSettings` gains `gates_enabled` (default true for k3s runs, false keeps M4 behaviour for the m4 flows), `max_fix_rounds`, `ci_timeout_s`, `hitl_poll_s` and `reviewer_model`.

## Left to the builder (plan)

- Task split and order (suggested: Gitea client → CI profile + seeding → ci MCP server → reviewer → gates driver (PR + CI + review) → fix loop → HITL + merge + policy → tracker stage comments → m5_flow + regressions → docs).
- Exact Gitea API payloads (verify against the live Gitea version on the stand).
- How the m5_flow plants the failing-test scenario deterministically. Suggested: the issue asks for a function plus a test, and a pre-seeded test in the repo fails until the function handles an edge case, so the first attempt is likely red. If the model gets it right first time, the flow forces a red CI by pushing a breaking commit before review and asserts that the fix round repairs it.

## Self-review notes (builder check done inline; no reviewer subagent was requested)

- **Answered by the project:**
  - Tracker loop transport: the task-source NodePort exists.
  - HITL storage: the agent_api `hitl_requests` table exists.
  - Evidence location: the M4 manifest.
- **Minor, marked as my calls:**
  - reviewer model and fallback;
  - max_fix_rounds = 2;
  - CI timeout of 20 min;
  - auto-merge classes limited to docs;
  - semgrep ruleset `p/ci`.
- **The owner's:** none blocking. The owner pre-authorized merge and release after a green quality gate and asked for questions only when critical (2026-11-15). The auto-merge policy for the platform's own repo follows spec §13.7 (HITL always).
