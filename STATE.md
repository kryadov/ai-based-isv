# STATE — ground truth for every agent session

> Read this file FIRST. Then `ARCH.md` (how it works, diagrams), `AGENTS.md` (rules), `TODO.md` (backlog).
> This file is updated at the end of EVERY increment. If your findings contradict it — verify, then update it.

**⚠️ WORKSTATION HANDOFF (2026-11-12, evening):** work moves to ANOTHER Windows
workstation (no GPU, 32 GB) and the LLM moves to an external model via Ollama.
Read `docs/superpowers/HANDOFF-2026-11-12.md` BEFORE anything else (session
context, every ruling from the gitignored ledger, final-review findings, next
steps); first message = `docs/superpowers/CONTINUE-PROMPT.md`.
**M4c is implemented on branch `feat/m4c-mcp-servers` (NOT merged):** Tasks 1–6
done, `m4c_flow.sh` PASS live (run 9); final review: I1 fixed, **I2+I3 open**
(read-only task-reader for sandboxes, netpol `from:`), then regressions → PR →
release v0.4.0 (owner approval). The live stand does not transfer — rebuild it
(INSTALL.md "Windows (WSL2)": `.wslconfig` memory + inotify, then
`org-bootstrap apply`).

Previous stand (2026-11-12, this GPU workstation): Windows/WSL2 + Rancher
Desktop, k3d 5.9.0/k3s v1.35.5, Ollama on the RTX 4070 (`OLLAMA_CONTEXT_LENGTH=16384`);
main's seven acceptance flows PASS there; macOS-era context:
`docs/superpowers/HANDOFF-2026-11-10.md`.

Last updated: 2026-11-12 evening (M4c implemented on feat/m4c-mcp-servers; review I2+I3 open; handoff to a no-GPU workstation + external LLM)

## What this project is

AI-native ISV: an internal platform where **swarms of AI agents develop, deploy and support software** for the organization. Input — a task in the tracker; output — a change in production (all-level tests, security, code review, canary rollout). Organization memory is shared across unrelated and related projects. Full design: `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md`; project lifecycle (onboard → develop → deliver → support → archive): spec §13; UI/automation addendum: `docs/superpowers/specs/2026-11-08-ui-and-automations-addendum.md`; how-it-works diagrams: `ARCH.md` / `ARCH.ru.md`.

## What works right now (verified on the live laptop stand)

The full L0 infrastructure stands up from scratch via `uv run org-bootstrap apply` (idempotent; config in `infra/`):

- **k3d cluster** `swarm-lab` (k3s v1.35, colima VM 6CPU/24GB; NodePort range widened to 10000-32767 — `infra/k3d/config.yaml`)
- **OpenBao** (key agent secret store; raft on PVC; bootstrap auto-init/unseal — keys in `infra/.vault-unseal-keys.json` + `.vault-root-token`, gitignored). Known caveat: `bao status` exits 2 while sealed — never gate on its exit code, grep the output; exec to the pod needs a self-retry loop (spec-agnostic race).
- **Postgres 17 + pgvector** (CNPG operator; DBs: gitea, openproject, litellm, swarm; host access via NodePort 15432 — `infra/postgres/nodeport.yaml`; do NOT use kubectl port-forward, it flaps)
- **Gitea** (git + PR + issues + OCI registry + act_runner; tracker v0 = Gitea Issues; admin token in OpenBao `secret/gitea/admin` field **token**)
- **Argo CD** (GitOps; demo app `podinfo` auto-synced to `prod`; podinfo logs requests only at debug — `PODINFO_LOG_LEVEL=debug` wired via env, args broke the image entrypoint)
- **kube-prometheus-stack** (Prometheus + Grafana :30082), **Loki + Alloy** (pod logs)
- **OpenProject** (deployed healthy; password/basic-auth login broken in v17-slim — see TODO)
- **LiteLLM gateway** (:30084) over host **Ollama** (qwen2.5-coder:7b, qwen3; LiteLLM needs its CNPG db `litellm` for master-key auth — wired via `DATABASE_URL`; host access via hostAliases pinned to the colima host IP 192.168.5.2, resolved dynamically in section_litellm; the pod only log-loads after ~15s — completion checks need retries)

Verified acceptance flows (run them on the stand):

```bash
./infra/tests/m2a_pr_flow.sh       # Gitea PR-flow with branch protection
./infra/tests/m2b_deploy_flow.sh   # GitOps demo-deploy → prod + metrics + logs
./infra/tests/m2c_flow.sh          # tracker issues → poller + LLM gateway
./infra/tests/m2d_flow.sh          # bootstrap CLI end-to-end
./infra/tests/m3_flow.sh           # control plane on live Postgres (NodePort, no port-forward)
./infra/tests/m4a_flow.sh          # agent_runtime E2E: issue-like vertex → agent in docker → PR branch with evidence
./infra/tests/m4b_flow.sh          # same vertex in a k3s sandbox pod: NetworkPolicy + Secret + NodePort + netpol negative
```

All six PASS as of this snapshot, plus `m4b_flow.sh` PASS (k3s sandbox vertex, ~4.5 min per run incl. pod spin-up). Code gates: `uv run ruff check .` / `uv run mypy packages apps` / `uv run pytest` — green (166 passed, 1 skipped).

**M4a adds — agent_runtime (first real executor, verified E2E):**

- **Executor contract** (`packages/agent_runtime/src/agent_runtime/executor.py`): `Executor.start(ExecutionRequest) -> ExecutionSession` (events/wait_done/diff/status/stop/result) — harness-agnostic; control plane never sees the harness.
- **OpenCode adapter**: thin REST/SSE client to `opencode serve` 2.2.0 (pinned `ghcr.io/anomalyco/opencode:2.2.0`; derived git-enabled image built via Dockerfile-on-stdin), docker launcher with Basic-auth password scraped from logs, eager SSE subscribe → pump into append-only NDJSON session log (torn-tail safe), run manifest (image pin, model, verdict), fail-closed on stream cuts.
- **Code-validated acceptance**: `done_when` (JSON checks: cmd/files_exist; empty/unparseable/unknown → FAIL) and `paths_allow` guard (deletions/renames/copies guarded both sides; unparseable diff → FAIL; per-section path attribution) — no LLM judgment.
- **Run-scoped worker** (`python -m agent_runtime.worker`): claim from `agent_api` (lease+heartbeat off-loop) → pristine clone + sandbox copy (`git clone --local`) → opencode in docker → diff → pathguard → apply to pristine clone → done_when → push `agent/vertex-{id}` with `--force` (token via `http.extraHeader`, never argv/URL/config) → complete/fail with verdict in audit; max-attempts blacklist; heartbeat-409 → abandon.
- **Evidence**: `.agent-runtime-workspace/run-N/{target/, sandbox/, .agent_runtime/runs/run-N/vertex-M/{session_log.ndjson,manifest.json}}` (gitignored).
- **Live lessons baked in**: model must be `litellm/qwen3` (qwen-coder returns tool-calls as text — preflight asserts `tool_calls`); bodyless `POST /api/session` → 400 (client sends `{}`); `Authorization: token <TOKEN>` header scheme confirmed live on Gitea git-http; done_when cmd runs on the host with a scrubbed env (PATH/HOME/LANG/TMPDIR; still host-side in M4b — the sandbox image has no Python);

**M4b adds — sandbox in k3s (verified E2E, `m4b_flow.sh` PASS):**

- **`agent_runtime/kube_sandbox.py`**: pod/service/secret lifecycle over a `kubectl` runner seam (same injection pattern as the docker spawn; no k8s SDK). Pod = initContainer (alpine/git clone via in-cluster `gitea-http.platform.svc.cluster.local:3000`, token via per-run Secret env — never argv) + serve container (derived `opencode-git:<tag>` image — **the base image has no git and the diff endpoint requires it**; PVC mounted; NO `OPENCODE_DB` — it breaks the harness snapshot machinery), NodePort service (range 30100–30110, allocated + conflict-retried), `reconcile_orphans` sweeps pods orphaned by dead workers. Teardown: Pod/Service/Secret idempotent (PVC persists). Evidence stays host-side (the adapter's SSE pump).
- **`vault_broker.py`**: read-only Gitea token from OpenBao via `kubectl exec … bao kv get` (m2a pattern); worker prefers explicit `AGENT_RUNTIME_GIT_TOKEN`.
- **`section_agents`** (bootstrap): namespace `agents`, PVC `agent-scratch` (5Gi, local-path — Pending until first consumer), default-deny + allow network policies; k3d NodePort range guard (`k3d cluster edit --port-add`, no cluster recreation).
- **Worker**: `AGENT_RUNTIME_SANDBOX_BACKEND=k3s` dispatch (`build_executor`); Vault-only path works E2E (token resolved BEFORE the first host git call); orphan reconcile at startup reclaims Pod+Service+Secret. v0 contract: ONE worker per run (age-based sweep is unsafe with concurrent workers). Docker path unchanged.
- **Live lessons**: NetPolicy IS enforced on k3d (negative tests: Postgres cluster :5432 and host :15432 denied; positives gitea/litellm in-cluster); one-shot probe pods must retry outbound checks (CNI warm-up transients) and a sleeping pod + `kubectl exec` is the stable probe pattern; opencode phones home to `models.opencode.ai` (non-fatal under the policy so far — WATCH in M4c: a free-gateway fallback was observed once under allow-all egress); OpenBao seals on host restarts — flows reading secrets must preflight/unseal (`infra/.vault-unseal-keys.json`); k3d publishes only individually mapped NodePorts — new ranges need `k3d cluster edit --port-add`; transient LiteLLM pod recreation (probable memory-pressure eviction during LLM load) can flake preflights — rerun.

## Milestones

- ✅ **M1** — monorepo skeleton (release **v0.1.0**)
- ✅ **M2a/M2b/M2c/M2d** — L0 infrastructure + bootstrapper CLI (release **v0.2.0**)
- ✅ **M3 — control plane (DONE)**: `isv_core` domain + `agent_api` (vertex queue with lease, identities, budgets, HITL inbox, audit journal, merge queue) — merged to main, **m3_flow PASS on live Postgres**, CI green, release **v0.3.0**. Plan: `docs/superpowers/plans/2026-11-09-m3-control-plane.md`.
- ✅ **M4a — Executor/OpenCode locally (DONE 2026-11-10)**: `agent_runtime` — Executor contract, OpenCode adapter (docker, pin 2.2.0), session log, done_when/paths_allow code validation, run manifest, run-scoped worker; **m4a_flow PASS ×2 live**, regressions m2a–d+m3 PASS, gates green. Plan: `docs/superpowers/plans/2026-11-09-m4a-executor-opencode.md`; design: `docs/superpowers/specs/2026-11-09-m4-agent-runtime-design.md`.
- ✅ **M4b — sandbox in k3s (DONE 2026-11-10)**: pod/service/secret lifecycle over kubectl seam, NetworkPolicy default-deny + allow (verified live), Vault-brokered read-only token, PVC workspace, per-attempt full clone, `reconcile_orphans`; **m4b_flow PASS live** (vertex → branch → verdict → teardown → netpol negative). Plan: `docs/superpowers/plans/2026-11-10-m4b-k3s-sandbox.md`; spike notes: `docs/superpowers/research/2026-11-10-m4b-*.md`.
- 🔄 **M4c — in progress on `feat/m4c-mcp-servers`** (implemented, m4c_flow PASS, review fixes I2+I3 open; MCP-серверы task-source/git/llm — FastMCP, streamable HTTP; полный E2E: Gitea issue → run → агент в поде → PR с evidence; петля статусов трекера In Progress → Done; затем release v0.4.0 = закрытие всего M4). Then M5 (gates + review), M6 (canary), M7 (memory), M8 (swarm mode), M9 (polyglot dogfood), M10 (UI panel + Automations).

## Releases / conventions

- Tags/releases per milestone, changelog from merged PRs: `./infra/scripts/release.sh v0.x.y` (spec §13.8). Published: **v0.1.0** (M1), **v0.2.0** (M2 close), **v0.3.0** (M3 close). Next: v0.4.0 at M4 close.
- Recursive self-development (platform builds itself): spec §13.7 — level 1 (meta-automations on our own logs) after M3, level 2 (this repo as a target project via tracker+pipeline) after M5/M6.
- External projects deliver tested release artifacts / Docker images to the org registry (spec §13.5); offboarding in v0 via `org-bootstrap offboard` (spec §13.6).
- Onboarding per project kind (§13.1): per-kind repo templates + tutorials planned (TODO), wizard v0 in CLI, Projects UI in M10 — confirmed by owner.

## Open decisions (owner)

- Executor model on the stand: `litellm/qwen3` (qwen-coder cannot drive the harness — tool-calls arrive as text; LiteLLM/Ollama translation quirk). Cloud API keys would lift both quality and speed of agent vertices.

- OpenProject auth quirk (password/basic auth broken) — investigate or OIDC; until then Gitea Issues is the tracker (TODO.md)
- LiteLLM providers: local Ollama now; cloud API keys when provided
- Visual brainstorm open Q's (UI panel layout details) — mockup screens in `.superpowers/brainstorm/` sessions (v0 CLI confirmed, kind table confirmed, offboarding v0 confirmed)

## Operational notes

- Repository is **public** on GitHub; the platform's own repo stays here, target projects get canonical repos in Gitea.
- **Source language rule: English only in code/comments; Russian exclusively in `*.ru.md` mirrors** (README.ru.md, INSTALL.ru.md, TODO.ru.md, ARCH.ru.md, AGENTS.ru.md). AGENTS.md is EN since 2026-11-09.
- Git discipline — AGENTS.md §9 (no `git add -A`, no cross-branch cherry-picks, no parallel bootstraps, artifacts gitignored before work).
- Do not recreate the k3d cluster unless needed — sections are idempotent; colima ≥6CPU/24GB. Network flakes happen: helm repo updates and completion checks are retry-wrapped in the sections.
