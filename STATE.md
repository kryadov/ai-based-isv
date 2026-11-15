# STATE — ground truth for every agent session

> Read this file FIRST. Then `ARCH.md` (how it works, diagrams), `AGENTS.md` (rules), `TODO.md` (backlog).
> This file is updated at the end of EVERY increment. If your findings contradict it — verify, then update it.

**Current (2026-11-16): M9 is CLOSED — polyglot dogfood; release v0.10.0.** The corridor is stack-agnostic: a `typescript` check profile (npm ci, tsc, eslint, node:test + the shared gitleaks/semgrep job) and a `ts-service` template (node:http, no framework) under `infra/templates/{check-profiles,projects}`; `org-bootstrap onboard --stack typescript --template ts-service` seeds the code before protection. Acceptance `infra/tests/m9_flow.sh` PASS live: `swarm-lab/ts-greeter` onboarded (seed CI green) → agent adds GET /api/time with a node:test test (done_when = node in docker) → PR #2 merged after CI + review + HITL → merged main green in a clean node container.
- Design `docs/superpowers/specs/2026-11-16-m9-polyglot-design.md`, plan `docs/superpowers/plans/2026-11-16-m9-polyglot.md`. Deferred: Java and React profiles, assistant eval profile, per-kind tutorials (TODO "M9 follow-ups").
- **Next: M10 (organization UI panel + Automations).** Usage: TUTORIAL.md §5.

**Earlier (2026-11-16): M8 closed — swarm mode; release v0.9.0.** A task now becomes a planned vertex graph executed by parallel agents: `swarm plan` (coordinator LLM; the platform validates the graph — acyclic, paths inside the allowed area, no shared files between concurrent vertices, platform-supplied done_when, single-vertex fallback), `swarm run --workers N` (separate lease identities and workspaces on the existing lease queue), handoffs from upstream vertices into dependent prompts, merge backpressure (no new claims while >= 3 merges wait for HITL), `swarm judge` (all done + merged main passes → run done + decision episode), control-plane `/metrics`. Acceptance `infra/tests/m8_flow.sh` PASS live: issue `isv-product#6` → 4 vertices (3 parallel + docs depending on them) → 3 workers (all three independent vertices overlapped) → every vertex merged through the corridor → handoff in the docs vertex prompt → judge done in 469 s.
- Design `docs/superpowers/specs/2026-11-16-m8-swarm-design.md`, plan `docs/superpowers/plans/2026-11-16-m8-swarm.md`. Found while wiring: the claim response never carried `deps` (fixed). Deferred: TODO "M8 follow-ups".

**Earlier (2026-11-16): M7 closed — project memory; release v0.8.0.** Experience now feeds the next task: vertex outcomes are written to project episodic memory (control-plane table `memory_episodes`, API `/memory/*`): deploy success markers, rollback postmortems, terminal failures, and up to two lessons from the light-model memory-extractor; at vertex start the worker puts the most relevant episodes (path overlap + task words) into the agent prompt and logs their ids (`memory_slice`). Org memory: Gitea `swarm-lab/org-memory` (`lessons/`, reuse catalog v1, docs check profile); `org-bootstrap memory promote --episode N` opens a human-reviewed PR. Acceptance `infra/tests/m7_flow.sh` PASS live: fault canary rolled back → postmortem episode #1 → the next agent task on `isv-product` (PR #5) received [1] in its prompt → promotion PR org-memory#1; the extractor added 2 lessons.
- Design `docs/superpowers/specs/2026-11-16-m7-memory-design.md`, plan `docs/superpowers/plans/2026-11-16-m7-memory.md`. Deferred (TODO "M7 follow-ups"): embeddings, code indexing, spec self-evals, auto-promotion, a `memory` MCP server.

**Earlier (2026-11-16): M6 closed — canary rollout with metric analysis; release v0.7.0.** A merged change to the demo service now reaches prod as an Argo Rollouts canary (20→50→100 %) and is judged by Prometheus: canary vs stable (the canary must carry traffic; 5xx ≤ max(2×stable, 5 %), p95 ≤ max(1.5×stable, 300 ms)) plus a post-rollout window against the pre-rollout baseline; a failed analysis aborts, prod returns to stable and a `postmortem.md` draft lands in the evidence. Acceptance `infra/tests/m6_flow.sh` PASS live: agent task `swarm-lab/isv-product#2` → corridor (PR #3, CI, review, HITL) → canary `isv-product:ca1fef1` promoted; same commit with `FAULT_ERROR_RATE=0.5` → `error-ratio` failed → rolled back, postmortem written, prod back on `ca1fef1`.
- Design `docs/superpowers/specs/2026-11-16-m6-canary-design.md`, plan `docs/superpowers/plans/2026-11-16-m6-canary.md`. Pieces: `apps/isv_product` (metrics, fault injection, Dockerfile); `infra/templates/deploy/service` (Rollout, analysis, k6); `bootstrap.sh rollouts` (argo-rollouts 2.43.8 + controller metrics) and `demo` (seeds `swarm-lab/isv-product`, Argo CD Application from Gitea); `agent_runtime.deployer` (+ CLI) and the worker deploy step (`AGENT_RUNTIME_DEPLOY_ENABLED`); `observability` MCP (read-only, Prometheus-only egress); Prometheus NodePort 30090; `deploy/**` is a protected gate path; HITL reject and canary rollback fail the vertex terminally.
- Live lessons: k6 keep-alive pinned traffic to old pods → the canary analysis passed vacuously (fixed: `canary-traffic` metric + no connection reuse); a same-image fault deploy reported success before Argo CD synced (fixed: verdict requires a new pod-template-hash); Gitea answers an existing branch protection with 403.


**Current (2026-11-15): spec §13.7 LEVEL 2 is ACHIEVED — the platform develops itself through its own corridor; release v0.6.0.** First self-made change: tracker issue `swarm-lab/ai-based-isv#2` → run 20 / vertex 20 → agent `swarm-agent` in a k3s sandbox → Gitea PR #3 → platform CI (`ci / checks`, `ci / security`) green → bot review clean → HITL approved (operator under the owner pre-authorization, after reading the diff) → merged (1069 s) → `platform-sync.sh promote` → GitHub PR #29 → GitHub CI green → merged `df0bed5` → `platform-sync.sh pull`. The change: `org-bootstrap check` no longer reports `[ok]` for a tool whose `--version` exits non-zero (+ tests) — TODO item closed by an agent.
- Level 2 design `docs/superpowers/specs/2026-11-15-selfdev-level2-design.md`, plan `docs/superpowers/plans/2026-11-15-selfdev-level2.md`; groundwork PR #28: agents use the NON-admin Gitea user `swarm-agent` (`bootstrap.sh agent-user`, OpenBao `secret/gitea/agent`, no admin fallback); bot reviews posted as COMMENT; platform check profile (repo-root `.gitea/`, `.platform/`, auto-merge off; the repo passes its own semgrep/gitleaks); `platform_repos` (always HITL); `infra/scripts/platform-sync.sh init|pull|promote`; `infra/tests/selfdev_flow.sh`. Workflow: AGENTS.md §11.
- Also 2026-11-15: **OpenRouter only on the z.ai plan limit** (PR #27, `infra/litellm/zai_limit_gate.py`). M5 (gates corridor) closed earlier the same day: [v0.5.0](https://github.com/kryadov/ai-based-isv/releases/tag/v0.5.0).
- **Next: M6 (canary rollout + post-rollout metric analysis)** — and from now on, suitable platform tasks go through the self-development corridor (AGENTS.md §11); lessons: run 19 failed on the flow's `done_when` (bare `uv run` without `--all-packages`), not on the agent.
- Open follow-ups: TODO.md "Self-development level 2 follow-ups", "M5 follow-ups", "M4c follow-ups".
- M4 closed earlier the same day: release [v0.4.0](https://github.com/kryadov/ai-based-isv/releases/tag/v0.4.0).
Work moved to a no-GPU Windows workstation:
- **Stand:** Rancher Desktop's built-in k3s plus the toolbox container (AGENTS.md §10; INSTALL.md "Windows (Rancher Desktop + toolbox)").
- **LLM:** external. Everything goes to the z.ai GLM Coding Plan; OpenRouter ONLY once the z.ai plan limit is exhausted, and keys go `.env` → OpenBao (AGENTS.md §10).
- **Final-review findings:** I1–I3 fixed. For I2+I3, sandboxes read tasks through the read-only `task-reader`, and NetworkPolicy keeps the write-capable `task-source` host-only.
- **Acceptance on the new stand:** see "Verified acceptance flows".
- **Previous move:** `docs/superpowers/HANDOFF-2026-11-12.md`.

Last updated: 2026-11-16 (M9 closed — v0.10.0; next M10)

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
  - `reviewer` → z.ai `glm-5.3` (fallback `reviewer-fallback` = OpenRouter `moonshotai/kimi-k3.1-pro`);
  - **OpenRouter ONLY on the z.ai plan limit** (owner directive, 2026-11-15): the proxy callback `infra/litellm/zai_limit_gate.py` sets `disable_fallbacks` on every request until z.ai answers HTTP 429; then z.ai is parked for 300 s and the fallbacks `qwen3-openrouter` (`z-ai/glm-5.3`), `qwen-coder-openrouter` (`deepseek/deepseek-v4.1-flash`), `reviewer-fallback` carry the traffic. Other z.ai errors are retried on z.ai (`retry_policy`) and never rerouted. Verified live: requests served by `api.z.ai` with 0 attempted fallbacks, a z.ai 400 stays a 400, `/active/callbacks` lists `ZaiLimitGate`;
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
./infra/tests/m5_flow.sh           # + gate corridor: PR → CI (checks + security) → bot review → HITL approve → merged → issue Done
```

**m5_flow PASS live (2026-11-15, run 11):** PR #2 merged; `ci / checks` and `ci / security` green; one bot review; HITL #2 approved; 131 s from claim to merge. Before the green run, the corridor found and blocked real defects in its own environment:
- act_runner pointed at the NodePort instead of the service port, and lacked the `ubuntu-latest` label;
- CI checkout used an unreachable ROOT_URL;
- semgrep crashed with pydantic ≥2.12;
- the profile itself had a mutable action tag and `curl | sh` installs, which semgrep blocked.

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

**M5 adds — the gate corridor (`feat/m5-gates`, verified E2E by `m5_flow.sh`):**
- **Check profile** (`infra/templates/check-profiles/python/`): a Gitea Actions workflow with two jobs.
  - `checks`: ruff, format, mypy, pytest.
  - `security`: gitleaks and semgrep `p/ci`.
  - Actions are SHA-pinned, downloads are checksum-verified, and there are no skip switches.
  - It ships with `.platform/merge-policy.yaml`. `org-bootstrap onboard` / `seed-demo` seed both, set `PLATFORM_GIT_URL` and protect `main` with both CI contexts.
- **Gate driver** (`agent_runtime/gates.py`), run by the worker after push when `AGENT_RUNTIME_GATES_ENABLED=1`:
  - PR (reused across attempts) → wait for CI → bot review → fix rounds (≤2) → HITL `merge` (or policy auto-merge for docs-only) → merge → confirm;
  - it fails closed on CI timeout, reviewer failure, protected paths (`.gitea/**`, `.platform/**`), HITL reject, or an unconfirmed merge;
  - `fix_rounds_exhausted` files a HITL `escalation`;
  - wiring is in `worker_gates.py`, which stops the implementer sandbox before fix rounds.
- **Reviewer** (`reviewer.py`, prompt `prompts/reviewer-v1.md`): LiteLLM alias `reviewer` → z.ai `glm-5.3` (OpenRouter only on the z.ai limit); the limit-time fallback `reviewer-fallback` is cross-family `moonshotai/kimi-k3.1-pro`. Output is structured JSON; invalid output gets one retry and is then treated as unavailable, which blocks the merge.
- **MCP `ci` server** (read-only `get_pr_checks` / `get_job_log`). The `agent_api` gets `GET /hitl/{id}`, plus `HITLClient` and `GiteaPRClient` (PRs, statuses, reviews, job-log tail, merge).
- **Tracker stage comments:** `[platform] PR opened / CI / review / fix round / merged`. Done carries the PR link.

## Milestones

- ✅ **M1** — monorepo skeleton (release **v0.1.0**)
- ✅ **M2a/M2b/M2c/M2d** — L0 infrastructure + bootstrapper CLI (release **v0.2.0**)
- ✅ **M3 — control plane (DONE)**: `isv_core` domain + `agent_api` (vertex queue with lease, identities, budgets, HITL inbox, audit journal, merge queue) — merged to main, **m3_flow PASS on live Postgres**, CI green, release **v0.3.0**. Plan: `docs/superpowers/plans/2026-11-09-m3-control-plane.md`.
- ✅ **M4a — Executor/OpenCode locally (DONE 2026-11-10)**. Plan: `docs/superpowers/plans/2026-11-09-m4a-executor-opencode.md`; design: `docs/superpowers/specs/2026-11-09-m4-agent-runtime-design.md`.
- ✅ **M4b — sandbox in k3s (DONE 2026-11-10)**. Plan: `docs/superpowers/plans/2026-11-10-m4b-k3s-sandbox.md`; spike notes: `docs/superpowers/research/2026-11-10-m4b-*.md`.
- ✅ **M4c — MCP servers + tracker status loop (DONE 2026-11-15)**: final review 0 Critical, I1–I3 fixed, minors in TODO.md; all eight flows PASS live on the Rancher stand; PR #25 merged; release **v0.4.0** closes M4. Plan: `docs/superpowers/plans/2026-11-11-m4c-mcp-servers.md`.
- ✅ **M5 — gates + review corridor (DONE 2026-11-15)**: `m5_flow` PASS live; regressions m2a–m4c all PASS; PR #26 merged; release **v0.5.0**. Plan: `docs/superpowers/plans/2026-11-15-m5-gates-review.md`.
- ⏭️ Then M6 (canary), M7 (memory), M8 (swarm mode), M9 (polyglot dogfood), M10 (UI panel + Automations). Spec §13.7 level 2 (this repo developed through its own corridor) becomes possible after M5/M6.

## Releases / conventions

- Tags/releases per milestone, changelog from merged PRs: `./infra/scripts/release.sh v0.x.y` (spec §13.8). Published: **v0.1.0** (M1), **v0.2.0** (M2 close), **v0.3.0** (M3 close), **v0.4.0** (M4 close, 2026-11-15), **v0.5.0** (M5 close, 2026-11-15), **v0.6.0** (§13.7 level 2: first self-made change, 2026-11-15). **v0.7.0** (M6 close, 2026-11-16), **v0.8.0** (M7 close, 2026-11-16), **v0.9.0** (M8 close, 2026-11-16), **v0.10.0** (M9 close, 2026-11-16). Next: v1.0.0 at M10 close.
- Recursive self-development (platform builds itself): spec §13.7 — level 1 (meta-automations on our own logs) after M3, level 2 (this repo as a target project via tracker+pipeline) — ACHIEVED 2026-11-15 (v0.6.0, AGENTS.md §11). Model/subscription routing research for that path: `docs/superpowers/research/2026-11-14-model-subscriptions-routing.md`.
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
