# STATE — ground truth for every agent session

> Read this file FIRST. Then `ARCH.md` (how it works, diagrams), `AGENTS.md` (rules), `TODO.md` (backlog).
> This file is updated at the end of EVERY increment. If your findings contradict it — verify, then update it.

Last updated: 2026-11-09 (pre-compaction snapshot)

## What this project is

AI-native ISV: an internal platform where **swarms of AI agents develop, deploy and support software** for the organization. Input — a task in the tracker; output — a change in production (all-level tests, security, code review, canary rollout). Organization memory is shared across unrelated and related projects. Full design: `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md`; project lifecycle (onboard → develop → deliver → support → archive): spec §13; UI/automation addendum: `docs/superpowers/specs/2026-11-08-ui-and-automations-addendum.md`; how-it-works diagrams: `ARCH.md` / `ARCH.ru.md`.

## What works right now (verified on the live laptop stand)

The full L0 infrastructure stands up from scratch via `uv run org-bootstrap apply` (idempotent; config in `infra/`):

- **k3d cluster** `swarm-lab` (k3s v1.35, colima VM 6CPU/24GB; NodePort range widened to 10000-32767 — `infra/k3d/config.yaml`)
- **OpenBao** (key agent secret store; raft on PVC; bootstrap auto-init/unseal — keys in `infra/.vault-unseal-keys.json` + `.vault-root-token`, gitignored). Known caveat: `bao status` exits 2 while sealed — never gate on its exit code, grep the output.
- **Postgres 17 + pgvector** (CNPG operator; DBs: gitea, openproject, litellm, swarm; host access via NodePort 15432 — `infra/postgres/nodeport.yaml`; do NOT use kubectl port-forward, it flaps)
- **Gitea** (git + PR + issues + OCI registry + act_runner; tracker v0 = Gitea Issues; admin token in OpenBao `secret/gitea/admin` field **token**)
- **Argo CD** (GitOps; demo app `podinfo` auto-synced to `prod`)
- **kube-prometheus-stack** (Prometheus + Grafana :30082), **Loki + Alloy** (pod logs)
- **OpenProject** (deployed healthy; password/basic-auth login broken in v17-slim — see TODO)
- **LiteLLM gateway** (:30084) over host **Ollama** (qwen2.5-coder:7b, qwen3; LiteLLM needs its CNPG db `litellm` for master-key auth — wired via `DATABASE_URL`)

Verified acceptance flows (run them on the stand):

```bash
./infra/tests/m2a_pr_flow.sh       # Gitea PR-flow with branch protection
./infra/tests/m2b_deploy_flow.sh   # GitOps demo-deploy → prod + metrics + logs
./infra/tests/m2c_flow.sh          # tracker issues → poller + LLM gateway
./infra/tests/m2d_flow.sh          # bootstrap CLI end-to-end
./infra/tests/m3_flow.sh           # control plane on live Postgres (NodePort, no port-forward)
```

Code gates: `uv run ruff check .` / `uv run mypy packages apps` / `uv run pytest` — green on every push (CI: GitHub Actions).

## Milestones

- ✅ **M1** — monorepo skeleton (release **v0.1.0**)
- ✅ **M2a/M2b/M2c/M2d** — L0 infrastructure + bootstrapper CLI (release **v0.2.0**)
- 🔄 **M3 — control plane (CURRENT, in flight)**: `isv_core` domain + `agent_api` store/API — code merged to main (35 unit tests green; endpoints: runs/vertices+lease, identities, budgets, HITL inbox, audit, merge queue; plan `docs/superpowers/plans/2026-11-09-m3-control-plane.md`).
  **In flight right now:** full L0 re-provision (fresh cluster; log `/tmp/bootstrap4.log`). **On completion run, in order:** `./infra/tests/m2a_pr_flow.sh` → m2b → m2c → m2d → `./infra/tests/m3_flow.sh` → then release **v0.3.0** via `./infra/scripts/release.sh v0.3.0` (convention: spec §13.8) and mark M3 done here.
- ⏭️ Next: **M4** (agent_runtime: Executor/OpenCode in k3s sandbox, MCP task-source/git/llm), **M5** (gates + review), **M6** (canary), **M7** (memory), **M8** (swarm mode), **M9** (polyglot dogfood), **M10** (UI panel + Automations). M2d done implies onboarding/management per §13 (spec: project lifecycle §13.1.5, repositories per kind + tutorials — TODO).

## Releases / conventions

- Tags/releases per milestone, changelog from merged PRs: `./infra/scripts/release.sh v0.x.y` (spec §13.8). Published: **v0.1.0** (M1), **v0.2.0** (M2 close). Next: v0.3.0 at M3 close.
- Recursive self-development (platform builds itself): spec §13.7 — level 1 (meta-automations on our own logs) after M3, level 2 (this repo as a target project via tracker+pipeline) after M5/M6.
- External projects deliver tested release artifacts / Docker images to the org registry (spec §13.5); offboarding in v0 via `org-bootstrap offboard` (spec §13.6).

## Open decisions (owner)

- OpenProject auth quirk (password/basic auth broken) — investigate or OIDC; until then Gitea Issues is the tracker (TODO.md)
- LiteLLM providers: local Ollama now; cloud API keys when provided
- Visual brainstorm open Q's (UI panel): mockup screens in `.superpowers/brainstorm/` (answers pending from owner: project kinds confirmed §13.5; offboarding v0 confirmed)

## Operational notes

- Repository is **public** on GitHub; the platform's own repo stays here, target projects get canonical repos in Gitea.
- **Source language rule: English only in code/comments; Russian exclusively in `*.ru.md` mirrors** (README.ru.md, INSTALL.ru.md, TODO.ru.md, ARCH.ru.md, AGENTS.ru.md). AGENTS.md is EN since 2026-11-09.
- Git discipline — AGENTS.md §9 (no `git add -A`, no cross-branch cherry-picks, no parallel bootstraps, artifacts gitignored before work).
- Do not recreate the k3d cluster unless needed — sections are idempotent; colima ≥6CPU/24GB.
