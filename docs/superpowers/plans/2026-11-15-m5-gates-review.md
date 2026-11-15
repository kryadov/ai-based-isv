# M5 Gates and Review Corridor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** turn an agent's pushed branch into a reviewed, CI-green, HITL-approved (or policy auto-merged) PR merged into the target repo, with tracker stage comments.

**Architecture:** a gate driver module (`agent_runtime/gates.py`) runs inside the host worker after a vertex succeeds. It talks to Gitea (PRs, commit statuses, reviews, merge), a reviewer (LiteLLM chat → structured JSON), agent_api (HITL) and the executor (fix attempts) only through narrow Protocol seams, so tests use fakes. CI runs in the target repo as a Gitea Actions workflow seeded from a per-stack check profile.

**Tech Stack:** Python 3.12, httpx, pydantic v2, FastMCP 4.1.8, FastAPI (agent_api), Gitea 1.2x Actions + act_runner, LiteLLM, bash acceptance flow.

**Spec:** `docs/superpowers/specs/2026-11-15-m5-gates-review-design.md` (authority: `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md` §7.2 item 4, §8 M5, §13.7)

## Global Constraints

- mypy --strict, ruff check + format, pytest; TDD (RED → GREEN) for every code task. Run them as `./infra/scripts/toolbox.sh uv run …`.
- English only in code, comments and docs; Russian only in `*.ru.md`.
- Tokens only in `Authorization: token …` headers. Every error message is redacted (`GiteaError` pattern). Never in argv, URLs or PR/tracker text.
- No skip switches: a failed or unknown CI state, reviewer error or timeout blocks the merge and never passes it.
- Implementer vertices may never change `.gitea/**` or `.platform/**`. The worker rejects a PR whose diff touches them before every merge.
- The platform's own repo: auto-merge disabled entirely (spec §13.7).
- Defaults: `max_fix_rounds=2`, `ci_timeout_s=1200`, `hitl_poll_s=15`, reviewer diff budget 60000 chars, auto-merge classes `docs/**` and `*.md`, reviewer alias `reviewer` → `openrouter/moonshotai/kimi-k3.1-pro` with fallback `openrouter/z-ai/glm-5.3`.
- Tracker comments are prefixed `[platform]`. Done is set only after a confirmed merge.
- Acceptance flows run one at a time (16 GB stand).

## Review Focus

1. CI never reports (workflow missing, runner down) → after `ci_timeout_s` the vertex is Blocked with "ci_timeout", and nothing is merged. Test in Task 6.
2. The reviewer returns non-JSON, or the LiteLLM call errors twice → the merge is blocked ("review_unavailable"), never auto-passed. Test in Task 4 (one retry, then error) and in Task 6 (the driver blocks).
3. A fix-round diff touches `.gitea/workflows/ci.yml` or `.platform/merge-policy.yaml` → rejected before apply, and the vertex is Blocked ("protected_path"). Tests in Task 5 and Task 6.
4. A retried vertex finds a PR already open for `agent/vertex-N` → it reuses that PR, with no duplicate and no 409 crash. Test in Task 1 (`ensure_pr`).
5. HITL rejected, or a worker stop while waiting → the PR is closed on reject; on stop the vertex is not completed and the PR stays open for the next attempt. Tests in Task 6.

---

### Task 1: Gitea PR / status / review client

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/gitea_prs.py`
- Test: `packages/agent_runtime/tests/test_gitea_prs.py`

**Interfaces:**
- Produces (all async, one `GiteaPRClient(http: httpx.AsyncClient, *, base_url: str, token: str)`; models are pydantic):
  - `PullRequest(number: int, url: str, head_sha: str, state: str, merged: bool)`
  - `ensure_pr(owner, repo, *, head: str, base: str, title: str, body: str) -> PullRequest`: returns the open PR for `head` if one exists, else creates it.
  - `get_pr(owner, repo, number) -> PullRequest`
  - `combined_status(owner, repo, sha) -> Literal["success","failure","error","pending"]`: Gitea `GET /repos/{o}/{r}/commits/{sha}/status`. No statuses at all → `"pending"`.
  - `changed_files(owner, repo, number) -> list[str]`
  - `pr_diff(owner, repo, number) -> str` (`GET /repos/{o}/{r}/pulls/{n}.diff`)
  - `post_review(owner, repo, number, *, body: str, comments: list[ReviewComment], event: Literal["COMMENT","REQUEST_CHANGES","APPROVED"]) -> None`
  - `ReviewComment(path: str, line: int, body: str)`
  - `comment(owner, repo, number, body: str) -> None` (issue comment on the PR)
  - `merge(owner, repo, number, *, message: str) -> None` (`Do: "merge"`)
  - `close(owner, repo, number) -> None`
  - `GiteaPRError(RuntimeError)`: the message never contains the token.

- [ ] **Step 1: Write failing tests** against a `httpx.MockTransport` fake Gitea (pattern: `packages/mcp_servers/tests/test_task_source.py` FakeGitea):
  - `test_ensure_pr_creates_when_none_open`: POST `/pulls` called once, returns number 3.
  - `test_ensure_pr_reuses_open_pr_for_head` (Review Focus 4): GET `/pulls?state=open` lists head `agent/vertex-5` → no POST, returns the existing PR.
  - `test_combined_status_maps_states` (parametrized: `success`, `failure`, `error`, `pending`, and an empty statuses list → `"pending"`).
  - `test_merge_sends_merge_style` (body has `"Do": "merge"`).
  - `test_errors_never_leak_token` (500 with the token echoed → `GiteaPRError` without the token).
- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: agent_runtime.gitea_prs`). Run: `./infra/scripts/toolbox.sh uv run pytest packages/agent_runtime/tests/test_gitea_prs.py -q`
- [ ] **Step 3: Implement** `gitea_prs.py` with the shape of `mcp_servers/gitea_client.py` (header auth, redaction).
- [ ] **Step 4: Run, expect PASS**; then `ruff check`, `ruff format`, `mypy packages apps`.
- [ ] **Step 5: Commit** `feat(m5): Gitea PR/status/review client`

### Task 2: Python check profile and seeding

**Files:**
- Create: `infra/templates/check-profiles/python/.gitea/workflows/ci.yml`
- Create: `infra/templates/check-profiles/python/.platform/merge-policy.yaml`
- Create: `apps/bootstrap/src/bootstrap/profiles.py`
- Test: `apps/bootstrap/tests/test_profiles.py`

**Interfaces:**
- Produces: `profile_files(stack: str) -> dict[str, str]` (repo path → content; `ValueError` for an unknown stack); `CI_CONTEXT: Final[str]`, the commit-status context name that branch protection requires (`"ci / checks (pull_request)"`: verify live in Task 9, and keep it in this one constant).
- `ci.yml`: on `pull_request` and `push` to main; job `checks` on `ubuntu-latest`:
  - install uv (pinned `0.13.8`), then `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy .`, `uv run pytest -q`;
  - job `security`: gitleaks (pinned binary `v8.28.0`, `detect --no-git --redact`) and `semgrep --config p/ci --error` (via `uvx semgrep==1.140.0`).
  - The workflow has no `continue-on-error` and no `if: always()` success masking.
- `merge-policy.yaml`: `auto_merge: {enabled: true, paths: ["docs/**", "*.md"]}`.

- [ ] **Step 1: Failing tests**:
  - `test_python_profile_has_workflow_and_policy`: the keys are exactly `.gitea/workflows/ci.yml` and `.platform/merge-policy.yaml`;
  - `test_workflow_runs_all_gates`: parse YAML; the steps' `run` text contains `ruff check`, `ruff format --check`, `mypy`, `pytest`, `gitleaks`, `semgrep`;
  - `test_workflow_has_no_skip_switches`: no `continue-on-error: true` anywhere;
  - `test_unknown_stack_raises`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** the templates plus `profiles.py`, reading them from `infra/templates/check-profiles/<stack>/` relative to the repo root (the same root discovery as `bootstrap.registry`).
- [ ] **Step 4: Run, expect PASS**; gates.
- [ ] **Step 5: Commit** `feat(m5): python check profile (CI + security) and merge policy templates`

### Task 3: MCP `ci` server

**Files:**
- Create: `packages/mcp_servers/src/mcp_servers/ci_server.py`
- Create: `infra/mcp/ci.yaml` (Deployment + ClusterIP `ci`, label `agent-runtime.io/sandbox-access: "yes"`, reuses Secret `mcp-task-source` for the Gitea token, port 8000)
- Modify: `infra/scripts/bootstrap.sh` `section_mcp` (apply `ci.yaml`, rollout `ci`)
- Test: `packages/mcp_servers/tests/test_ci_server.py`, extend `apps/bootstrap/tests/test_mcp_manifests.py`

**Interfaces:**
- Consumes: Task 1 `GiteaPRClient.combined_status`, `get_pr`.
- Produces: `create_ci_app(*, base_url: str, token: str, http: httpx.AsyncClient | None = None) -> FastMCP` with read-only tools:
  - `get_pr_checks(pr_ref: str) -> {"state": str, "statuses": [{context, state, target_url}]}`, where `pr_ref` = `owner/repo!N`;
  - `get_job_log(pr_ref: str, context: str, tail_lines: int = 200) -> str`: Actions job log tail via the status `target_url`. Unavailable → a short explanatory string, never an exception carrying the token.
- `parse_pr_ref(ref: str) -> tuple[str, str, int]` (`ValueError` on bad input).

- [ ] **Step 1: Failing tests**:
  - `test_tools_are_read_only`: tool names are exactly `{get_pr_checks, get_job_log}`;
  - `test_get_pr_checks_reports_state`;
  - `test_parse_pr_ref` (good `o/r!3`; bad `o/r#3`, `o/r!x`);
  - `test_ci_manifest_is_sandbox_accessible` (in `test_mcp_manifests.py`).
- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Implement** with the `task_source.py` patterns (stateless_http, json_response, token-free errors); `main()` reads `GITEA_URL`/`GITEA_TOKEN` like task-source. Add the image build to `section_mcp` the same way as the other servers.
- [ ] **Step 4: PASS**; gates.
- [ ] **Step 5: Commit** `feat(m5): read-only ci MCP server`

### Task 4: Reviewer

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/reviewer.py`
- Create: `packages/agent_runtime/src/agent_runtime/prompts/reviewer-v1.md`
- Modify: `infra/litellm/deployment.yaml` (alias `reviewer` → `openrouter/moonshotai/kimi-k3.1-pro`; `reviewer-fallback` → `openrouter/z-ai/glm-5.3`; router fallback `reviewer: ["reviewer-fallback"]`)
- Test: `packages/agent_runtime/tests/test_reviewer.py`, extend `apps/bootstrap/tests/test_litellm_manifest.py`

**Interfaces:**
- Produces:
  - `Finding(path: str, line: int, severity: Literal["blocking","nit"], body: str)`
  - `Review(summary: str, findings: list[Finding], model: str, prompt_version: str, truncated: bool)`, with property `blocking: list[Finding]`
  - `class ReviewUnavailable(RuntimeError)`
  - `async review_diff(http: httpx.AsyncClient, *, base_url: str, api_key: str, model: str, goal: str, diff: str, budget_chars: int = 60000) -> Review`. The call is POST `{base_url}/v1/chat/completions` with `response_format={"type":"json_object"}`. Invalid JSON or a schema mismatch gets exactly one retry, then `ReviewUnavailable`. HTTP errors → `ReviewUnavailable` without the key. A diff over budget is cut at a hunk boundary with `truncated=True`. `prompt_version = "reviewer-v1"`.

- [ ] **Step 1: Failing tests**:
  - `test_review_parses_findings`;
  - `test_invalid_json_retries_once_then_unavailable` (Review Focus 2: assert exactly 2 calls);
  - `test_http_error_is_unavailable_without_key`;
  - `test_large_diff_truncated_flag` (diff 70k → `truncated is True`, the sent content ≤ 60000 + prompt);
  - `test_reviewer_alias_cross_family` (manifest: `reviewer` model starts with `openrouter/moonshotai/`; fallback configured).
- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Implement.** The prompt file asks for JSON `{summary, findings:[{path,line,severity,body}]}`. `blocking` is reserved for correctness, security and spec violations; style is `nit`.
- [ ] **Step 4: PASS**; gates.
- [ ] **Step 5: Commit** `feat(m5): cross-family LLM reviewer with structured findings`

### Task 5: Merge policy and protected paths

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/merge_policy.py`
- Test: `packages/agent_runtime/tests/test_merge_policy.py`

**Interfaces:**
- Produces:
  - `PROTECTED_GLOBS: Final[tuple[str, ...]] = (".gitea/**", ".platform/**")`
  - `touches_protected(paths: Sequence[str]) -> list[str]`
  - `MergePolicy(auto_merge_enabled: bool, auto_merge_paths: list[str])`
  - `load_policy(text: str | None, *, platform_repo: bool) -> MergePolicy`: `None` or invalid text → auto-merge disabled; `platform_repo=True` → disabled regardless
  - `auto_merge_allowed(paths: Sequence[str], policy: MergePolicy) -> bool`: True only if `paths` is non-empty and every path matches. Globs via `fnmatch` with `**` support, the same matcher as `pathguard`.

- [ ] **Step 1: Failing tests**:
  - `test_touches_protected` (Review Focus 3: `.gitea/workflows/ci.yml` and `.platform/merge-policy.yaml` flagged; `src/x.py` not);
  - `test_docs_only_auto_merge`;
  - `test_mixed_paths_need_hitl`;
  - `test_platform_repo_never_auto_merges`;
  - `test_missing_or_invalid_policy_disables_auto_merge`;
  - `test_empty_paths_never_auto_merge`.
- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Implement**, reusing the `pathguard` glob helper (import it, do not copy).
- [ ] **Step 4: PASS**; gates.
- [ ] **Step 5: Commit** `feat(m5): merge policy and protected gate paths`

### Task 6: Gate driver

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/gates.py`
- Test: `packages/agent_runtime/tests/test_gates.py`

**Interfaces:**
- Consumes: Task 1 client (as the Protocol `PRPort`), Task 4 `review_diff`/`Review`/`ReviewUnavailable` (via a `ReviewPort` callable), Task 5 policy functions.
- Produces:
  - `GateConfig(max_fix_rounds: int = 2, ci_timeout_s: float = 1200, ci_poll_s: float = 10, hitl_poll_s: float = 15, platform_repo: bool = False)`
  - `GateOutcome(status: Literal["merged","blocked","stopped"], pr_url: str, reason: str = "", rounds: int = 0, review_summary: str = "", auto_merged: bool = False)`
  - Protocols:
    - `PRPort`: the Task 1 methods used;
    - `ReviewPort = Callable[[str, str], Awaitable[Review]]` (goal, diff);
    - `FixPort = Callable[[FixRequest], Awaitable[FixResult]]`, with `FixRequest(round: int, ci_log_tail: str, blocking: list[Finding])` and `FixResult(pushed: bool, replies: dict[str, str], reason: str = "")`;
    - `HITLPort`: `create(kind: str, subject: str, context: dict) -> int` and `status(request_id: int) -> Literal["pending","approved","rejected"]`;
    - `NotifyPort = Callable[[str], Awaitable[None]]` (tracker stage comment; failures are swallowed by the caller).
  - `async run_gates(*, owner: str, repo: str, branch: str, base: str, title: str, body: str, goal: str, policy_text: str | None, pr: PRPort, review: ReviewPort, fix: FixPort, hitl: HITLPort, notify: NotifyPort, cfg: GateConfig, stop: asyncio.Event, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> GateOutcome`
- Algorithm (the order is load-bearing):
  1. `ensure_pr` → notify "PR opened: <url>".
  2. Loop rounds 0..`max_fix_rounds`:
     - re-read the PR, check `changed_files` with `touches_protected` → blocked `protected_path`;
     - wait for CI: poll `combined_status(head_sha)` until success, failure or error. Over the timeout → blocked `ci_timeout`. On stop → `stopped`;
     - review the diff. `ReviewUnavailable` → blocked `review_unavailable`;
     - post the review: event `REQUEST_CHANGES` if anything is blocking, else `COMMENT`;
     - notify the CI state and the review summary;
     - if CI is success and nothing is blocking → leave the loop;
     - if this is the last round → blocked `fix_rounds_exhausted`;
     - else call `fix(FixRequest(...))`. `pushed=False` → blocked with `FixResult.reason`. Post `replies` as PR comments; notify "fix round N".
  3. `auto_merge_allowed(changed_files, load_policy(policy_text, platform_repo=cfg.platform_repo))` → merge, `auto_merged=True`. Else HITL `create("merge", pr_url, {pr_url, review_summary, rounds, ci: "success"})` and poll `status`: approved → merge; rejected → `close`, blocked `hitl_rejected`; stop → `stopped`, and the PR stays open.
  4. After merge: `get_pr` must report `merged=True`, else blocked `merge_unconfirmed`. Notify "merged: <url>".

- [ ] **Step 1: Failing tests** with in-memory fakes and an instant `sleep`:
  - `test_happy_path_hitl_approved_merges`;
  - `test_docs_only_auto_merges_without_hitl`;
  - `test_platform_repo_always_hitl`;
  - `test_red_ci_triggers_fix_round_then_merges` (the fake turns CI green after the fix);
  - `test_blocking_review_triggers_fix_and_replies_posted`;
  - `test_fix_rounds_exhausted_blocks`;
  - `test_ci_timeout_blocks_without_merge` (Review Focus 1);
  - `test_review_unavailable_blocks` (Review Focus 2);
  - `test_protected_path_blocks_before_review` (Review Focus 3);
  - `test_hitl_rejected_closes_pr`;
  - `test_stop_while_waiting_hitl_leaves_pr_open` (Review Focus 5);
  - `test_merge_unconfirmed_blocks`.
  - Every blocked test asserts `merge` was never called.
- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Implement** `gates.py` (no I/O beyond the ports).
- [ ] **Step 4: PASS**; gates.
- [ ] **Step 5: Commit** `feat(m5): gate driver — PR, CI, review, fix rounds, HITL, merge`

### Task 7: agent_api HITL read endpoint + worker HITL adapter

**Files:**
- Modify: `apps/agent_api/src/agent_api/main.py` (add `GET /hitl/{request_id}` → `{id, kind, subject, status, context}`; 404 if unknown)
- Create: `packages/agent_runtime/src/agent_runtime/hitl_client.py` (`HITLClient(http, base_url)` implementing Task 6 `HITLPort`)
- Test: `apps/agent_api/tests/test_hitl.py` (extend or create, following the existing agent_api test style), `packages/agent_runtime/tests/test_hitl_client.py`

**Interfaces:**
- Produces: `HITLClient.create(kind, subject, context) -> int` (POST `/hitl`, reads `id` from the response; verify the key name in `main.py` `create_hitl`), `HITLClient.status(request_id) -> str`.

- [ ] **Step 1: Failing tests**: `test_get_hitl_returns_status_after_decision`, `test_get_hitl_unknown_404`, `test_hitl_client_round_trip` (against the FastAPI app via `httpx.ASGITransport`).
- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: PASS**; gates.
- [ ] **Step 5: Commit** `feat(m5): HITL read endpoint and worker HITL client`

### Task 8: Worker wiring (gates after push, fix attempts, stage comments)

**Files:**
- Modify: `packages/agent_runtime/src/agent_runtime/settings.py` (new fields: `gates_enabled: bool = False`, `max_fix_rounds: int = 2`, `ci_timeout_s: float = 1200`, `hitl_poll_s: float = 15`, `reviewer_model: str = "reviewer"`, `gitea_url: str = ""`, `platform_repo: bool = False`)
- Modify: `packages/agent_runtime/src/agent_runtime/worker.py` (`_process_vertex`: after `push_head`, if `gates_enabled` → `run_gates`; `complete` only on `merged`, otherwise `client.fail(reason)` plus a HITL `escalation` for `fix_rounds_exhausted`; the fix port re-runs the executor with `build_fix_prompt`)
- Modify: `packages/agent_runtime/src/agent_runtime/pathguard.py` usage: the implementer's `paths_allow` evaluation also rejects `PROTECTED_GLOBS`
- Test: `packages/agent_runtime/tests/test_worker_gates.py`

**Interfaces:**
- Consumes: Tasks 1, 4, 5, 6, 7.
- Produces: `build_fix_prompt(vertex: dict[str, Any], req: FixRequest) -> str`, which includes the CI log tail (≤ 200 lines) and every blocking finding as `path:line — body`, and asks for a final JSON `{"replies": {"<path>:<line>": "<reply>"}}`. `parse_fix_replies(text: str) -> dict[str, str]` returns `{}` on garbage.
- Behaviour: with `gates_enabled=False`, M4 behaviour is byte-for-byte unchanged (m4a/m4b/m4c flows keep passing).

- [ ] **Step 1: Failing tests**:
  - `test_gates_disabled_keeps_m4_completion` (the existing fake-executor pattern from `test_worker.py`);
  - `test_gates_merged_completes_vertex`;
  - `test_gates_blocked_fails_vertex_with_reason`;
  - `test_fix_rounds_exhausted_files_escalation`;
  - `test_implementer_diff_touching_protected_paths_rejected` (Review Focus 3, at the pathguard level);
  - `test_build_fix_prompt_contains_findings_and_log`;
  - `test_parse_fix_replies_tolerates_garbage`.
- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Implement.** The gate-side Gitea token comes from the existing `resolve_gitea_token`; `policy_text` is read from the pushed repo checkout (`.platform/merge-policy.yaml`) at the base branch, never from the agent's diff.
- [ ] **Step 4: PASS**, plus the full suite; gates.
- [ ] **Step 5: Commit** `feat(m5): worker runs the gate corridor after push`

### Task 9: Live corridor: bootstrap wiring, m5_flow, regressions, docs

**Files:**
- Modify: `infra/scripts/bootstrap.sh` (`section_mcp` already covers ci from Task 3; `section_litellm` serves `reviewer`)
- Create: `infra/tests/m5_flow.sh` (copy the m4c preflight and worker launch; seed `target-scratch` with the Task 2 profile files plus branch protection requiring `CI_CONTEXT`; worker env `AGENT_RUNTIME_GATES_ENABLED=1`)
- Modify: `STATE.md`, `ARCH.md`/`ARCH.ru.md` (pipeline diagram: PR → CI → review → HITL → merge), `INSTALL.md`/`INSTALL.ru.md` (m5_flow, the approve command), `TODO.md`/`TODO.ru.md`

**m5_flow assertions** (one after another, each a `fail` on miss):
1. A PR exists for `agent/vertex-N`, and the tracker has a `[platform] PR opened` comment.
2. The CI commit status for the PR head is `success`, with a `security` job present.
3. The PR has a review from the platform bot, and the tracker has a review summary comment.
4. A HITL `merge` request is pending. The flow approves it via `POST /hitl/{id}/decision`.
5. The PR is merged into main, and main contains the agent's files.
6. The issue is closed, and the last comment carries the PR URL and `verdict: PASS`.
7. Scenario B (fix loop): the flow pushes a breaking commit to the PR branch (a failing assert in `tests/`) before review. The CI goes red, and a fix round is recorded (`[platform] fix round 1`). The CI ends `success` and the PR merges after approval.

- [ ] **Step 1:** Write `m5_flow.sh`. Run it live: `./infra/scripts/toolbox.sh bash infra/tests/m5_flow.sh`. Expected: `M5 flow: PASS`. Fix real defects found live (TDD per defect, unit test first). Verify `CI_CONTEXT` against the live Gitea status context and adjust the Task 2 constant.
- [ ] **Step 2:** Regressions one at a time: m2a, m2b, m2c, m2d, m3, m4a, m4b, m4c. Expected: all PASS.
- [ ] **Step 3:** Docs: STATE.md (M5 status, flows), ARCH (+ru) pipeline, INSTALL (+ru) approve command, TODO (+ru).
- [ ] **Step 4:** Gates, then commit `feat(m5): m5_flow live corridor + docs`. Push, open the PR, CI green, merge, `./infra/scripts/release.sh v0.5.0` (owner pre-authorized after a green quality gate).
