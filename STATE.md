# STATE — ground truth for every agent session

> Read this file FIRST. Then `ARCH.md` (how it works, diagrams), `AGENTS.md` (rules), `TODO.md` (backlog).
> This file is updated at the end of EVERY increment. If your findings contradict it — verify, then update it.

**Current (2026-11-15): M4c is on branch `feat/m4c-mcp-servers`, PR to main, awaiting the owner's merge.**
Work moved to a no-GPU Windows workstation:
- **Stand:** Rancher Desktop's built-in k3s plus the toolbox container (AGENTS.md §10; INSTALL.md "Windows (Rancher Desktop + toolbox)").
- **LLM:** external. The z.ai GLM Coding Plan comes first, OpenRouter is the fallback on plan limits, and keys go `.env` → OpenBao (AGENTS.md §10).
- **Final-review findings:** I1–I3 fixed. For I2+I3, sandboxes read tasks through the read-only `task-reader`, and NetworkPolicy keeps the write-capable `task-source` host-only.
- **Acceptance on the new stand:** see "Verified acceptance flows".
- **After merge:** `./infra/scripts/release.sh v0.4.0` closes M4, only with owner approval.
- **Previous move:** `docs/superpowers/HANDOFF-2026-11-12.md`.

Last updated: 2026-11-15 (M4c review fixes, Rancher/toolbox stand, z.ai + OpenRouter LLM, flows re-verified live)

## What this project is

AI-native ISV: an internal platform where **swarms of AI agents develop, deploy and support software** for the organization. Input — a task in the tracker; output — a change in production (all-level tests, security, code review, canary rollout). Organization memory is shared across unrelated and related projects. Full design: `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md`; project lifecycle (onboard → develop → deliver → support → archive): spec §13; UI/automation addendum: `docs/superpowers/specs/2026-11-08-ui-and-automations-addendum.md`; how-it-works diagrams: `ARCH.md` / `ARCH.ru.md`.

## What works right now (verified on the live stand)

The full L0 infrastructure stands up via `org-bootstrap apply` / `infra/scripts/bootstrap.sh <section>` (idempotent; config in `infra/`). On the Windows stand, run every command through `./infra/scripts/toolbox.sh` (AGENTS.md §10).

- **Cluster:** Rancher Desktop's built-in k3s v1.37 (moby engine), `SWARM_CLUSTER=rancher-desktop`. Images built with `docker build` are visible to k3s, and every NodePort is forwarded to localhost. `infra/rancher/swarm-lab.start` sets the NodePort range 10000-32767 and the inotify limits. The k3d flavour (`SWARM_CLUSTER=k3d`, the default) still works for macOS/Linux.
- **OpenBao** (key agent secret store; raft on PVC; bootstrap auto-init/unseal — keys in `infra/.vault-unseal-keys.json` + `.vault-root-token`, gitignored). **It comes up sealed after every Rancher restart — run the `vault` section first.** `bao status` exits 2 while sealed — grep the output, never gate on its exit code.
- **Postgres 17 + pgvector** (CNPG operator; DBs: gitea, openproject, litellm, swarm; host access via NodePort 15432; do NOT use kubectl port-forward, it flaps)
- **Gitea** (git + PR + issues + OCI registry + act_runner; tracker v0 = Gitea Issues; admin token in OpenBao `secret/gitea/admin` field **token**)
- **Argo CD** (GitOps; demo app `podinfo` auto-synced to `prod`; `PODINFO_LOG_LEVEL=debug` via env)
- **kube-prometheus-stack** (Prometheus + Grafana :30082). **Loki + Alloy** (pod logs). On moby, Alloy also mounts `/var/lib/docker/containers`, because `/var/log/pods/*` are symlinks there.
- **OpenProject** (deployed healthy; password/basic-auth login broken in v17-slim — see TODO)
- **LiteLLM gateway** (:30084; master key in OpenBao `secret/litellm/master`; CNPG db `litellm`):
  - aliases: `qwen3` (agent model, real tool_calls) → z.ai `glm-5.3`; `qwen-coder` (light, thinking disabled) → z.ai `glm-5.3-flash`;
  - on failure or plan limit, router fallbacks go to `qwen3-openrouter` (`z-ai/glm-5.3`) and `qwen-coder-openrouter` (`deepseek/deepseek-v4.1-flash`), and z.ai is cooled down for 300 s;
  - keys: `.env` → OpenBao `secret/litellm/{zai,openrouter}` → Secret `litellm-provider`;
  - the section waits with `rollout status`, not `wait Available`.
- **Agent sandboxes** (namespace `agents`: PVC, default-deny + allow NetworkPolicies) and **MCP servers** (FastMCP, streamable HTTP):
  - `task-source`: tracker read/write; holds the Gitea admin token; host-only via NodePort 30085, and NetworkPolicy admits no pod;
  - `task-reader`: read-only tools; reachable from sandboxes;
  - `llm`: LiteLLM proxy; reachable from sandboxes.
  - Sandboxes may reach only pods labelled `agent-runtime.io/sandbox-access: "yes"`.

Verified acceptance flows (run them through the toolbox: `./infra/scripts/toolbox.sh bash infra/tests/<flow>.sh`):

```bash
./infra/tests/m2a_pr_flow.sh       # Gitea PR-flow with branch protection
./infra/tests/m2b_deploy_flow.sh   # GitOps demo-deploy → prod + metrics + logs
./infra/tests/m2c_flow.sh          # tracker issues → poller + LLM gateway
./infra/tests/m2d_flow.sh          # bootstrap CLI end-to-end
./infra/tests/m3_flow.sh           # control plane on live Postgres (NodePort, no port-forward)
./infra/tests/m4a_flow.sh          # agent_runtime E2E: issue-like vertex → agent in docker → PR branch with evidence
./infra/tests/m4b_flow.sh          # same vertex in a k3s sandbox pod: NetworkPolicy + Secret + NodePort + netpol negative
./infra/tests/m4c_flow.sh          # Gitea issue → run → agent in pod via MCP → branch + evidence → tracker In Progress → Done; MCP netpol (I2/I3)
```

Status on the Rancher/toolbox stand (2026-11-15): **all eight PASS**, run one at a time:
- m2a, m2b, m2c, m2d, m3, m4a, m4b, m4c.
- m4c includes the I2/I3 NetworkPolicy evidence: the reader lists no write tools; `task-source` is denied from `agents` and from another namespace; host NodePort 30085 works.
- The OpenRouter fallback aliases also return real `tool_calls` (checked directly).
- m4b used to fail on the old stand's local qwen3 model quality; on glm-5.3 via z.ai it passes.

Code gates: `ruff check` / `ruff format --check` / `mypy packages apps` / `pytest` are green (prefix each with `./infra/scripts/toolbox.sh uv run`).

**M4a adds — agent_runtime (first real executor, verified E2E):**

- **Executor contract** (`packages/agent_runtime/src/agent_runtime/executor.py`): `Executor.start(ExecutionRequest) -> ExecutionSession` (events/wait_done/diff/status/stop/result) — harness-agnostic; control plane never sees the harness.
- **OpenCode adapter**: thin REST/SSE client to `opencode serve` 2.2.0 (pinned `ghcr.io/anomalyco/opencode:2.2.0`; derived git-enabled image built via Dockerfile-on-stdin), docker launcher with Basic-auth password scraped from logs, eager SSE subscribe → pump into append-only NDJSON session log (torn-tail safe), run manifest (image pin, model, verdict), fail-closed on stream cuts.
- **Code-validated acceptance**: `done_when` (JSON checks: cmd/files_exist; empty/unparseable/unknown → FAIL) and `paths_allow` guard (deletions/renames/copies guarded both sides; unparseable diff → FAIL; per-section path attribution) — no LLM judgment.
- **Run-scoped worker** (`python -m agent_runtime.worker`): claim from `agent_api` (lease+heartbeat off-loop) → pristine clone + sandbox copy (`git clone --local`) → opencode in docker → diff → pathguard → apply to pristine clone → done_when → push `agent/vertex-{id}` with `--force` (token via `http.extraHeader`, never argv/URL/config) → complete/fail with verdict in audit; max-attempts blacklist; heartbeat-409 → abandon.
- **Evidence**: `.agent-runtime-workspace/run-N/{target/, sandbox/, .agent_runtime/runs/run-N/vertex-M/{session_log.ndjson,manifest.json}}` (gitignored).
- **Live lessons baked in**:
  - the executor model must emit real `tool_calls`; the preflight asserts it;
  - a bodyless `POST /api/session` gets 400 (the client sends `{}`);
  - the `Authorization: token <TOKEN>` header scheme is confirmed live on Gitea git-http;
  - done_when cmd runs on the host with a scrubbed env (PATH/HOME/LANG/TMPDIR).

**M4b adds — sandbox in k3s (verified E2E, `m4b_flow.sh` PASS):**

- **`agent_runtime/kube_sandbox.py`**: pod/service/secret lifecycle over a `kubectl` runner seam (same injection pattern as the docker spawn; no k8s SDK). Pod = initContainer (alpine/git clone via in-cluster `gitea-http.platform.svc.cluster.local:3000`, token via per-run Secret env — never argv) + serve container (derived `opencode-git:<tag>` image — **the base image has no git and the diff endpoint requires it**; PVC mounted; NO `OPENCODE_DB` — it breaks the harness snapshot machinery), NodePort service (range 30100–30110, allocated + conflict-retried), `reconcile_orphans` sweeps pods orphaned by dead workers. Teardown: Pod/Service/Secret idempotent (PVC persists; deletion is asynchronous — wait for it). Evidence stays host-side (the adapter's SSE pump).
- **`vault_broker.py`**: read-only Gitea token from OpenBao via `kubectl exec … bao kv get` (m2a pattern); worker prefers explicit `AGENT_RUNTIME_GIT_TOKEN`.
- **`section_agents`** (bootstrap): namespace `agents`, PVC `agent-scratch`, default-deny + allow network policies; on k3d the NodePort range guard runs `k3d cluster edit --port-add` (it is a no-op on Rancher Desktop).
- **Worker**: `AGENT_RUNTIME_SANDBOX_BACKEND=k3s` dispatch (`build_executor`); Vault-only path works E2E; orphan reconcile at startup reclaims Pod+Service+Secret. v0 contract: ONE worker per run. Docker path unchanged.
- **Live lessons**:
  - NetworkPolicy is enforced (negatives: Postgres denied; MCP `task-source` denied from pods);
  - one-shot probe pods must retry outbound checks; a sleeping pod plus `kubectl exec` is the stable probe pattern;
  - opencode phones home to `models.opencode.ai` (non-fatal under the policy);
  - OpenBao seals on host restarts.

**M4c adds — MCP servers (implemented on `feat/m4c-mcp-servers`):** `packages/mcp_servers`:
- `task-source` / `task-reader`: Gitea Issues tools `get_task`, `list_tasks`, plus `set_task_status` and `add_task_comment` on the writer only;
- `llm`: LiteLLM proxy;
- `git`: built, wired in M5.

The worker drives the tracker status loop (In Progress → Done/Blocked with evidence comments) through the host-only `task-source`. Sandboxes get the read-only `task-reader` and `llm` as OpenCode MCP servers.

## Milestones

- ✅ **M1** — monorepo skeleton (release **v0.1.0**)
- ✅ **M2a/M2b/M2c/M2d** — L0 infrastructure + bootstrapper CLI (release **v0.2.0**)
- ✅ **M3 — control plane (DONE)**: `isv_core` domain + `agent_api` (vertex queue with lease, identities, budgets, HITL inbox, audit journal, merge queue) — merged to main, **m3_flow PASS on live Postgres**, CI green, release **v0.3.0**. Plan: `docs/superpowers/plans/2026-11-09-m3-control-plane.md`.
- ✅ **M4a — Executor/OpenCode locally (DONE 2026-11-10)**. Plan: `docs/superpowers/plans/2026-11-09-m4a-executor-opencode.md`; design: `docs/superpowers/specs/2026-11-09-m4-agent-runtime-design.md`.
- ✅ **M4b — sandbox in k3s (DONE 2026-11-10)**. Plan: `docs/superpowers/plans/2026-11-10-m4b-k3s-sandbox.md`; spike notes: `docs/superpowers/research/2026-11-10-m4b-*.md`.
- 🔄 **M4c — MCP servers + tracker status loop**:
  - implemented; final review: 0 Critical, I1–I3 fixed, minors in TODO.md;
  - live acceptance on the new stand: all eight flows PASS (see above);
  - PR `feat/m4c-mcp-servers` → main;
  - after merge, release **v0.4.0** closes M4 (owner approval).
  - Then M5 (gates + review), M6 (canary), M7 (memory), M8 (swarm mode), M9 (polyglot dogfood), M10 (UI panel + Automations).

## Releases / conventions

- Tags/releases per milestone, changelog from merged PRs: `./infra/scripts/release.sh v0.x.y` (spec §13.8). Published: **v0.1.0** (M1), **v0.2.0** (M2 close), **v0.3.0** (M3 close). Next: v0.4.0 at M4 close.
- Recursive self-development (platform builds itself): spec §13.7 — level 1 (meta-automations on our own logs) after M3, level 2 (this repo as a target project via tracker+pipeline) after M5/M6. Model/subscription routing research for that path: `docs/superpowers/research/2026-11-14-model-subscriptions-routing.md`.
- External projects deliver tested release artifacts / Docker images to the org registry (spec §13.5); offboarding in v0 via `org-bootstrap offboard` (spec §13.6).
- Onboarding per project kind (§13.1): per-kind repo templates + tutorials planned (TODO), wizard v0 in CLI, Projects UI in M10 — confirmed by owner.

## Open decisions (owner)

- **z.ai Coding Plan behind LiteLLM.** The z.ai ToS lists supported tools only, and LiteLLM is not on the list. The owner chose z.ai first anyway (2026-11-15). If the plan gets restricted, route z.ai only through the OpenCode provider; OpenRouter stays the metered path.
- OpenProject auth quirk (password/basic auth broken) — investigate or OIDC; until then Gitea Issues is the tracker (TODO.md)
- Visual brainstorm open Q's (UI panel layout details) — mockup screens in `.superpowers/brainstorm/` sessions (v0 CLI confirmed, kind table confirmed, offboarding v0 confirmed)

## Operational notes

- Repository is **public** on GitHub; the platform's own repo stays here, target projects get canonical repos in Gitea.
- **Source language rule: English only in code/comments; Russian exclusively in `*.ru.md` mirrors** (README.ru.md, INSTALL.ru.md, TODO.ru.md, ARCH.ru.md, AGENTS.ru.md).
- Git discipline — AGENTS.md §9 (no `git add -A`, no cross-branch cherry-picks, no parallel bootstraps, artifacts gitignored before work).
- Secrets: `.env` (gitignored, template `.env.example`) → OpenBao. Never commit keys, and never print their values; check presence instead.
- Memory is tight on the 16 GB Rancher VM: run the acceptance flows one at a time.
- Windows host gotchas: the `python3` on Windows PATH is a Store stub (it hangs), so run Python only in the toolbox. Smart App Control blocks native uv Python. Write `.sh` files with LF.
