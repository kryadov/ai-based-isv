# STATE — ground truth for every agent session

> Read this file FIRST. Then `ARCH.md` (how it works, diagrams), `AGENTS.md` (rules), `TODO.md` (backlog).
> This file is updated at the end of EVERY increment. If your findings contradict it — verify, then update it.

Last updated: 2026-11-09 (Task 6 — thin opencode serve client)

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
```

All five PASS as of this snapshot. Code gates: `uv run ruff check .` / `uv run mypy packages apps` / `uv run pytest` — green (85 passed, 1 skipped). M4 agent_runtime: thin `opencode serve` HTTP+SSE client implemented in `packages/agent_runtime/src/agent_runtime/opencode_client.py` with full `MockTransport` test coverage.

## Milestones

- ✅ **M1** — monorepo skeleton (release **v0.1.0**)
- ✅ **M2a/M2b/M2c/M2d** — L0 infrastructure + bootstrapper CLI (release **v0.2.0**)
- ✅ **M3 — control plane (DONE)**: `isv_core` domain + `agent_api` (vertex queue with lease, identities, budgets, HITL inbox, audit journal, merge queue) — merged to main, **m3_flow PASS on live Postgres**, CI green, release **v0.3.0**. Plan: `docs/superpowers/plans/2026-11-09-m3-control-plane.md`.
- ⏭️ **Next: M4** (agent_runtime: Executor/OpenCode in k3s sandbox, MCP task-source/git/llm) — write the plan at increment start (`docs/superpowers/plans/` convention). Then M5 (gates + review), M6 (canary), M7 (memory), M8 (swarm mode), M9 (polyglot dogfood), M10 (UI panel + Automations).

## Releases / conventions

- Tags/releases per milestone, changelog from merged PRs: `./infra/scripts/release.sh v0.x.y` (spec §13.8). Published: **v0.1.0** (M1), **v0.2.0** (M2 close), **v0.3.0** (M3 close). Next: v0.4.0 at M4 close.
- Recursive self-development (platform builds itself): spec §13.7 — level 1 (meta-automations on our own logs) after M3, level 2 (this repo as a target project via tracker+pipeline) after M5/M6.
- External projects deliver tested release artifacts / Docker images to the org registry (spec §13.5); offboarding in v0 via `org-bootstrap offboard` (spec §13.6).
- Onboarding per project kind (§13.1): per-kind repo templates + tutorials planned (TODO), wizard v0 in CLI, Projects UI in M10 — confirmed by owner.

## Open decisions (owner)

- OpenProject auth quirk (password/basic auth broken) — investigate or OIDC; until then Gitea Issues is the tracker (TODO.md)
- LiteLLM providers: local Ollama now; cloud API keys when provided
- Visual brainstorm open Q's (UI panel layout details) — mockup screens in `.superpowers/brainstorm/` sessions (v0 CLI confirmed, kind table confirmed, offboarding v0 confirmed)

## Operational notes

- Repository is **public** on GitHub; the platform's own repo stays here, target projects get canonical repos in Gitea.
- **Source language rule: English only in code/comments; Russian exclusively in `*.ru.md` mirrors** (README.ru.md, INSTALL.ru.md, TODO.ru.md, ARCH.ru.md, AGENTS.ru.md). AGENTS.md is EN since 2026-11-09.
- Git discipline — AGENTS.md §9 (no `git add -A`, no cross-branch cherry-picks, no parallel bootstraps, artifacts gitignored before work).
- Do not recreate the k3d cluster unless needed — sections are idempotent; colima ≥6CPU/24GB. Network flakes happen: helm repo updates and completion checks are retry-wrapped in the sections.
