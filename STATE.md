# STATE — ground truth for every agent session

> Read this file FIRST. Then `AGENTS.md` (rules), `TODO.md` (backlog).
> This file is updated at the end of EVERY increment. If your findings contradict it — verify, then update it.

Last updated: 2026-11-09

## What this project is

AI-native ISV: an internal platform where **swarms of AI agents develop, deploy and support software** for the organization. Input — a task in the tracker; output — a change in production (all-level tests, security, code review, canary rollout). Organization memory is shared across unrelated and related projects. Full design: `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md`; project lifecycle (onboard → develop → deliver → support → archive): spec §13; UI/automation addendum: `docs/superpowers/specs/2026-11-08-ui-and-automations-addendum.md`.

## What works right now (verified on the live laptop stand)

The full L0 infrastructure stands up from scratch via `uv run org-bootstrap apply` (idempotent; config in `infra/`):

- **k3d cluster** `swarm-lab` (k3s v1.35, colima VM 6CPU/24GB; see `infra/k3d/config.yaml`)
- **OpenBao** (raft on PVC, auto-init/unseal; secrets: infra/.vault-unseal-keys.json + .vault-root-token, gitignored)
- **Postgres 17 + pgvector** (CNPG operator; DBs: gitea, openproject, litellm, swarm)
- **Gitea** (git + PR + issues + OCI registry + act_runner; tracker v0 = Gitea Issues)
- **Argo CD** (GitOps; demo app `podinfo` auto-synced to `prod` namespace)
- **kube-prometheus-stack** (Prometheus + Grafana :30082), **Loki + Alloy** (pod logs)
- **OpenProject** (deployed healthy; password/auth login broken in v17-slim — see TODO)
- **LiteLLM gateway** (:30084) over host **Ollama** (qwen2.5-coder:7b; models qwen-coder, qwen3)

Verified acceptance flows (run them on the stand):

```bash
./infra/tests/m2a_pr_flow.sh       # Gitea PR-flow with branch protection
./infra/tests/m2b_deploy_flow.sh   # GitOps demo-deploy → prod + metrics + logs
./infra/tests/m2c_flow.sh          # tracker issues → poller → platform + LLM gateway
./infra/tests/m2d_flow.sh          # bootstrap CLI end-to-end
```

Code quality gates: `uv run ruff check .` / `uv run mypy packages apps` / `uv run pytest` — green on every push (CI: GitHub Actions).

## Milestones

- ✅ **M1** — monorepo skeleton (uv workspace, 5 packages + 3 apps, AGENTS.md v2, ADR-0001..0003)
- ✅ **M2a/M2b/M2c/M2d** — L0 infrastructure + bootstrapper CLI (all acceptance PASS, merged)
- 🔄 **M3 — control plane (CURRENT)**: `isv_core` domain models + `agent_api` (vertex queue with lease, identities, budgets, HITL inbox, audit journal, merge queue). **Code merged to main** (unit-tested: 35 tests, trio green; plan: `docs/superpowers/plans/2026-11-09-m3-control-plane.md`). Remaining: live-Postgres acceptance (`infra/tests/m3_flow.sh` against CNPG `swarm` DB) — then M3 done.
- ⏭️ Next: **M4** (agent_runtime: Executor/OpenCode in k3s sandbox, MCP task-source/git/llm), **M5** (gates + review), **M6** (canary), **M7** (memory), **M8** (swarm mode), **M9** (polyglot dogfood), **M10** (UI panel + Automations).

## Open decisions (owner)

- OpenProject auth quirk (password/basic auth broken) — investigate or add OIDC plugin; until then Gitea Issues is the tracker (TODO.md)
- LiteLLM model providers: local Ollama now; add cloud API keys when the owner provides them
- Visual brainstorm (UI panel layout) — mockups live in `.superpowers/brainstorm/` sessions; open questions in the last screen (CLI→UI, project types, offboarding v0)

## Operational notes

- Repository is **public** on GitHub; canonical repo of the platform itself stays here, target projects get canonical repos in Gitea.
- Source code Standard: **English only** in code (comments/strings); Russian lives in `*.ru.md` mirrors and internal RU design docs (see TODO: translate spec/plans to EN).
- Do not recreate the k3d cluster unless needed — `infra/scripts/bootstrap.sh <section>` is idempotent; colima must have ≥6CPU/24GB.

## Decisions log (append-only, owner-approved)

- 2026-11-08: project types confirmed (§13.5) — SaaS / internal service / tool / assistant; **external projects deliver a tested release artifact or Docker image to the org registry** (no canary; release-candidate acceptance instead).
- 2026-11-08: project offboarding — implemented in v0 via bootstrap CLI (`org-bootstrap offboard`), not deferred.
- 2026-11-08: onboarding wizard — v0 in CLI (apps/bootstrap), full 📁 Projects UI in M10.
