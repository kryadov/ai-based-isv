# AI-Native ISV — Swarm Agent Development Platform

An organization where **swarms of AI agents develop, deploy and support software**.
The goal of the organization is the full lifecycle of projects — products, internal
services, tools and assistants: **onboarding → development → implementation → support**
(spec §13).

What that means concretely:

- **Input: a task in the tracker.** Output: a change in production — with all-level
  tests, security scanning, code review and a canary rollout.
- **Swarm execution**: a coordinator plans a task graph; specialist agents (architecture,
  security, testing) review; executor agents implement vertices in isolated k3s
  sandboxes. One writer per code area; verification capacity, not generation, is the
  scaling limit.
- **Quality gates everywhere**: CI without bypasses, bot review + human review,
  evidence packs attached to every merge, deterministic canary analysis.
- **Organization memory**: lessons, a reuse catalog and a "constitution" shared across
  unrelated projects; project memory per repo.
- **Automathons (recurring workflows)**: vulnerability scans, dependency updates, log
  analysis, customer-feedback ingestion — the same swarm machinery, event/schedule
  triggered, results land in a human approval inbox.

> Русская версия: [README.ru.md](README.ru.md)

## Current state

**v1.0.0 — the full delivery loop runs on a laptop stand** (Rancher Desktop k3s +
toolbox), every milestone proven by a live acceptance flow:

- **L0** (OpenBao, Postgres+pgvector, Gitea + Actions, Argo CD + Rollouts,
  Prometheus/Grafana, Loki, LiteLLM → z.ai Coding Plan; OpenRouter only when
  its limit is exhausted), provisioned by one idempotent command.
- **The corridor:** tracker issue → agent in a k3s sandbox → PR → CI (per-stack
  check profile + security scans) → bot review → fix rounds → **HITL** → merge
  → **canary rollout with metric analysis** (auto-rollback + postmortem).
- **Project memory** (lessons into the next task's prompt) and **org memory**
  (lessons + reuse catalog via PRs).
- **Swarm mode:** a planned vertex graph executed by parallel agents.
- **Polyglot:** Python, TypeScript.
- **Self-development:** the platform develops itself through its own corridor.
- **Organization panel** (`/ui`): Inbox decisions, Swarm, Projects, Memory,
  Metrics, Automations.

Start with [TUTORIAL.md](TUTORIAL.md); the authoritative status is [STATE.md](STATE.md).


## Layers

| Layer | What | Where |
|---|---|---|
| **L0** | organization infrastructure as code: Gitea, OpenProject, OpenBao, Postgres, Argo CD, LiteLLM, observability | `infra/` + `apps/bootstrap` |
| **L1** | the agent platform: swarm control plane, memory, quality gates, evidence packs | `packages/`, `apps/agent_api` |
| **L2** | end-to-end delivery: tracker task → prod | the pipeline, target projects |

## How to run (dev)

```bash
git clone https://github.com/kryadov/ai-based-isv && cd ai-based-isv
uv sync --all-packages
uv run ruff check . && uv run mypy packages apps && uv run pytest
```

Acceptance flows for the L0 stand: `./infra/tests/m2a_pr_flow.sh`,
`./infra/tests/m2b_deploy_flow.sh`, `./infra/tests/m2c_flow.sh`,
`./infra/tests/m2d_flow.sh` (details in [INSTALL.md](INSTALL.md)).

## Project map

| File | Purpose |
|---|---|
| [STATE.md](STATE.md) | **ground truth: current state, milestones, next steps** — read first in every session |
| [ARCH.md](ARCH.md) | architecture with diagrams: topology, pipeline, swarm, memory, control plane |
| [AGENTS.md](AGENTS.md) | agent rules (Superpowers mandate, standards) |
| [TUTORIAL.md](TUTORIAL.md) | **start here**: use the platform on the stand — watch a task end to end, approve HITL, onboard your project, give tasks to agents |
| [INSTALL.md](INSTALL.md) | setup: prerequisites, dev env, L0 bootstrap |
| [TODO.md](TODO.md) | backlog and feedback loops |
| [docs/superpowers/specs/](docs/superpowers/specs/) | full platform design + addenda |
| [docs/adr/](docs/adr/) | architecture decisions (stack, Executor, memory) |
| docs/superpowers/plans/ | per-milestone implementation plans |
