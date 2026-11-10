# M4c: MCP servers (task-source/git/llm) + full tracker-loop E2E — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The three first MCP servers (task-source, git, llm) as FastMCP streamable-HTTP services, the tracker status feedback loop closed in both directions (claim → In Progress → Done/Blocked), and the full platform cycle E2E: Gitea issue → run → k3s agent → PR with evidence → tracker statuses. Release v0.4.0 closes milestone M4.

**Architecture:** `packages/mcp_servers` gains three FastMCP servers. task-source and llm deploy IN-CLUSTER (namespace `agents`, reachable from sandbox pods under the existing netpol — add an explicit allow for the MCP port) and are registered into the harness config (`opencode_config_text` gains `mcp.servers.*`). The git MCP v0 runs WORKER-SIDE (in-process with the worker, audited) wrapping the existing gitops operations — the sandbox still never sees write credentials (M4a ruling; agent-driven git waits for per-project scoped tokens, M5). The worker calls task-source over HTTP to update tracker statuses; `Run.task_ref` (M3) carries `owner/repo#number`.

**Tech Stack:** FastMCP (streamable HTTP), httpx, pydantic v2; existing M3/M4a/M4b contracts (agent_api, kube_sandbox, gitops, worker).

**Spec:** `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md` §7.1 (MCP inventory), `docs/superpowers/plans/2026-11-10-m4b-k3s-sandbox.md` review notes (same-pod vs separate-pod MCP), TODO.md tracker feedback loop, AGENTS.md §9.

## Global Constraints

- After every task green: `uv run ruff check .` · `uv run mypy packages apps` (zero suppressions) · `uv run pytest`.
- The sandbox NEVER receives git write credentials (M4a ruling stands); the git MCP is worker-side v0.
- English only; explicit-path commits, no `git add -A` (AGENTS.md §9).
- Live-stand patterns: OpenBao reads via `kubectl exec … bao kv get` (unseal-guarded per m4b lesson); in-cluster services (`gitea-http.platform.svc:3000`, `litellm.platform.svc:4000/v1`); NodePort for host access.
- FastMCP goes into `packages/mcp_servers` deps only (`fastmcp>=2.x`); agent_runtime/agent_api do NOT depend on it.

## Review Focus

1. **Tracker loop silent-failure** — if set_status fails, the issue must not stay silently "In Progress": failures are audited and the worker logs them, but a tracker failure NEVER fails the vertex (tracker is observability, not acceptance). Test: Task 5.
2. **MCP server holding the admin token** — token read at startup from OpenBao, kept in memory only, never logged/echoed; the Gitea adapter redacts it from errors. Test: Task 2.
3. **Harness actually sees the MCP servers** — a session started after `opencode_config_text` gains `mcp.servers` must have the tools available; the acceptance asserts a harness event showing the MCP tool was used OR the config schema is validated. Test: Task 4/6.
4. **Poller → run dedup** — the m2c checkpoint logic must not create duplicate runs for the same issue. Test: Task 6 (re-run → no second run).
5. **status mapping discipline** — only the platform's own transitions (In Progress on claim, Done on verdict, Blocked on fail) — no arbitrary statuses. Test: Task 5.

---

### Task 1: Spike — FastMCP in-cluster + harness registration (throwaway)

**Files:**
- Create: `docs/superpowers/research/2026-11-11-m4c-fastmcp-spike.md`

**Interfaces:**
- Produces: (a) minimal FastMCP streamable-HTTP server that boots in a k3s pod and answers a tool call from the host; (b) the exact `mcp.servers.*` config shape opencode 2.2.0 accepts (url vs command, and whether a session log shows the MCP tool call); (c) netpol note: which port the agents→MCP allow rule needs; (d) latency/reliability gotchas.

- [ ] **Step 1:** Write a toy FastMCP server (`echo` tool), run it on the host (uv run), call it via the streamable-HTTP client; then run it as a pod in `agents` (python3.12-slim + fastmcp pip-installed at boot — image building is NOT part of the spike) and call it from the host via NodePort.
- [ ] **Step 2:** Register it in a local `opencode.jsonc` (`mcp.servers`), start `opencode serve` (docker, M4a style), ask the model to call the `echo` tool, capture the harness event proving the MCP round-trip.
- [ ] **Step 3:** Write the note; commit only the note. Clean up pods/containers.

### Task 2: task-source MCP server (Gitea Issues adapter)

**Files:**
- Create: `packages/mcp_servers/pyproject.toml` (deps: fastmcp, httpx; dev: httpx2 mirror), `packages/mcp_servers/src/mcp_servers/task_source.py`, `packages/mcp_servers/src/mcp_servers/gitea_client.py`
- Modify: root `pyproject.toml` (workspace members already include packages/* — verify)
- Test: `packages/mcp_servers/tests/test_task_source.py`, `packages/mcp_servers/tests/test_gitea_client.py`

**Interfaces:**
- Produces: `GiteaIssuesClient(http: httpx.AsyncClient, *, base_url: str, token: str)` — `list_issues(owner, repo, state="open") -> list[IssueSummary]`, `get_issue(owner, repo, number) -> IssueDetail`, `set_state(owner, repo, number, state: "open"|"closed") -> None`, `add_comment(owner, repo, number, body: str) -> None`; errors → `GiteaError(RuntimeError)` with the token REDACTED. FastMCP server `create_task_source_app(*, base_url: str, token: str) -> FastMCP` exposing tools: `get_task(task_ref)`, `list_tasks(task_ref_prefix)`, `set_task_status(task_ref, status: "open"|"in-progress"|"done"|"blocked", note: str = "")` (maps in-progress→open+label? NO — v0: Gitea state open/closed + a status COMMENT carrying the platform status; issue closed ⇔ done), `add_task_comment(task_ref, body)`. `task_ref` format `owner/repo#42`.
- Behavior: every tool call is auditable (the SERVER logs op+task_ref without the token); unknown task_ref → tool error (not crash).

- [ ] **Step 1: Failing tests** — client: list/get/set_state/add_comment against httpx MockTransport (assert paths `/api/v1/repos/{owner}/{repo}/issues[...]`, auth header `token`, token redaction in errors); server: tool mapping via the FastMCP in-memory client (`fastmcp.Client(server)`), status→state mapping (done→closed, in-progress→open+comment), redaction, unknown-ref error.
- [ ] **Step 2:** RED run. **Step 3:** Implement. **Step 4:** GREEN + gates. **Step 5:** Commit explicit paths.

### Task 3: git MCP (worker-side, audited) + llm MCP (LiteLLM proxy)

**Files:**
- Create: `packages/mcp_servers/src/mcp_servers/git_server.py`, `packages/mcp_servers/src/mcp_servers/llm_server.py`
- Test: `packages/mcp_servers/tests/test_git_server.py`, `packages/mcp_servers/tests/test_llm_server.py`

**Interfaces:**
- Consumes: `agent_runtime.gitops` (apply_diff/push_head/ensure_repo signatures) — imported lazily (worker-side only; mcp_servers must not hard-depend on agent_runtime → define a narrow Protocol `GitOpsLike` in git_server and adapt in the worker).
- Produces: `create_git_server(gitops: GitOpsLike) -> FastMCP` with tools `clone_status()`, `apply_and_push(run_id, vertex_id, diff, work_branch, message)` (wraps apply_diff+push_head; audit note in the tool result), errors surfaced as tool errors; `create_llm_server(*, base_url: str, api_key: str) -> FastMCP` with tool `complete(prompt: str, model: str = "", max_tokens: int = 256)` proxying LiteLLM `/v1/chat/completions` (api key never logged; timeout 600 s).

- [ ] **Step 1: Failing tests** (git: fake GitOpsLike records calls, result dict shape, error mapping; llm: MockTransport, auth header, key redaction). **Step 2:** RED. **Step 3:** Implement. **Step 4:** GREEN + gates. **Step 5:** Commit.

### Task 4: Deploy task-source + llm in-cluster; register MCP in the harness config

**Files:**
- Create: `infra/mcp/task-source.yaml` (Deployment+Service NodePort 30085→container 8000, env GITEA base url/token from OpenBao via... v0: env from a k8s Secret created by the flow/bootstrap), `infra/mcp/llm.yaml` (Deployment+Service, env LITELLM base/key)
- Modify: `infra/scripts/bootstrap.sh` (`section_mcp`: apply manifests; idempotent), `apps/bootstrap/src/bootstrap/cli.py` (SECTIONS + help)
- Modify: `packages/agent_runtime/src/agent_runtime/opencode_executor.py` (`opencode_config_text(*, model, llm_endpoint, mcp_servers: dict[str, dict[str, str]] | None = None)` — emits `"mcp": {"servers": {...url...}}`; worker settings gain `mcp_task_source_url`/`mcp_llm_url` (empty = omit); KubeSandbox passes them through (SandboxExecutorFactory params)
- Test: `packages/agent_runtime/tests/test_opencode_executor.py` (config text with/without mcp), `packages/mcp_servers/tests/…` unchanged

**Interfaces:**
- Produces: in-cluster `task-source.agents.svc:<port>` and `llm.agents.svc:<port>`; sandbox pods reach them (netpol allow: add TCP port to `infra/agents/netpol-allow.yaml` — the MCP service port inside `agents` namespace is IN-namespace traffic: netpol ingress must allow the sandbox→MCP flow explicitly).

- [ ] **Step 1: Failing config test** (config text contains mcp.servers urls when given, absent when None). **Step 2:** RED. **Step 3:** Implement + manifests + section. **Step 4:** GREEN + gates + `org-bootstrap apply --section mcp` live check. **Step 5:** Commit.

### Task 5: Tracker status loop in the worker

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/tracker.py` (thin httpx client for the task-source MCP server: `set_status(task_ref, status, note)`, `add_comment(task_ref, body)` — plain HTTP, NOT fastmcp-dependent)
- Modify: `packages/agent_runtime/src/agent_runtime/worker.py` (`WorkerSettings`: + `task_ref: str = ""`, `task_source_url: str = ""`; on claim → set_status in-progress; after complete → done + comment with branch/evidence refs; after fail → blocked + reason; failures logged+audited, never fatal)
- Test: `packages/agent_runtime/tests/test_tracker_loop.py`

**Interfaces:**
- Consumes: task-source HTTP API (FastMCP streamable HTTP — the client speaks plain HTTP JSON-RPC to `/mcp` per the spike's documented shape).
- Produces: `TrackerClient(http, base_url)` — used by worker_loop hooks; `task_ref` empty → loop disabled (backward compat).

- [ ] **Step 1: Failing tests** — MockTransport: claim → in-progress call made; complete → done + comment contains branch; fail → blocked + reason; tracker outage → vertex still completes; empty task_ref → no calls. **Step 2:** RED. **Step 3:** Implement. **Step 4:** GREEN + gates. **Step 5:** Commit.

### Task 6: Acceptance m4c_flow.sh — issue → run → agent → PR → statuses; release v0.4.0

**Files:**
- Create: `infra/tests/m4c_flow.sh`
- Modify: `infra/scripts/bootstrap.sh` (register `m4c` needs? no — mcp section covers), `ARCH.md` (verification map), `STATE.md` (increment close — controller)
- Create: release `v0.4.0` via `./infra/scripts/release.sh v0.4.0` AFTER the flow passes

**Interfaces:**
- Consumes: everything above; m2c poller pattern (`infra/tests/task_source_poller.py`) adapted to POST `/runs` with `task_ref=owner/repo#N` (a small inline python in the flow, checkpoint-deduped).

- [ ] **Step 1:** Write `m4c_flow.sh` (style of m4b): preflight (stand, section mcp, OpenBao unseal, tool_calls probe) → create Gitea issue `M4c: ...` in `swarm-lab/target-scratch` → run the adapted poller → assert a run exists with `task_ref=swarm-lab/target-scratch#N` → seed a vertex on that run → worker (k3s backend, task_ref + task_source_url set) → poll for vertex done → assert: branch in Gitea; the ISSUE state is closed AND its last comment contains the branch name and the verdict; negative netpol probe retained → cleanup.
- [ ] **Step 2:** Run → PASS (attach tail). Regressions: m2a–d, m3, m4a, m4b → PASS. Gates → clean.
- [ ] **Step 3:** `./infra/scripts/release.sh v0.4.0` → tag pushed; changelog entry present.
- [ ] **Step 4:** Commit explicit paths.

## Self-review notes

- Design §7.1 coverage: task-source (T2+T5), git (T3, worker-side v0 — the RULING: agent-driven git waits for scoped tokens M5; ledger + plan amendment), llm (T3), deployment+registration (T4), full E2E with the tracker loop (T6). Release v0.4.0 closes M4.
- The m2c poller stays as-is (test infra); the m4c flow embeds its own small run-creation step (checkpoint-deduped) rather than changing m2c's contract.
- Out of scope: per-project tokens (M5), MCP for deploy/observability/memory (later milestones), OpenProject tracker (TODO).
