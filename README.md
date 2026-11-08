# AI-Native ISV — Swarm Agent Development Platform

An internal agent-driven software development platform: **input** — a task in the tracker, **output** — a change in production (all-level tests, security, code review, canary rollout). Swarm agent orchestration (coordinators → specialists → executors in isolated sandboxes) with project and organization memory.

> Русская версия: [README.ru.md](README.ru.md)

## Documentation

| File | What it is |
|---|---|
| [INSTALL.md](INSTALL.md) | Setup: prerequisites, dev environment, L0 bootstrap |
| [AGENTS.md](AGENTS.md) | Agent working rules (Superpowers mandate) |
| [docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md](docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md) | Full platform design |
| [docs/adr/](docs/adr/) | Architecture decisions (stack, Executor, memory) |
| [docs/superpowers/plans/](docs/superpowers/plans/) | Increment plans (M1, M2a, M2b, …) |
| [TODO.md](TODO.md) | Roadmap items / feedback loops |

## Layers

- **L0** — organization infrastructure as code (`infra/`): Gitea, OpenProject, Vault, Postgres, Argo CD, LiteLLM, observability (Prometheus/Grafana/Loki). Provisioned by `infra/bootstrap.sh` on a clean machine.
- **L1** — the platform (`packages/`, `apps/agent_api`): swarm orchestrator, memory, quality gates, evidence packs.
- **L2** — end-to-end delivery (`apps/isv_product` — demo project).

## Quick commands

```bash
uv sync --all-packages      # environment
uv run ruff check .         # lint
uv run mypy packages apps   # strict typing
uv run pytest               # tests
```

Increment status: M1 ✓ (skeleton) · M2a ✓ (L0 core: k3d + Vault + Postgres + Gitea) · M2b ✓ (Argo CD + Prometheus/Grafana + Loki, demo via GitOps in prod) · next: M2c (OpenProject + LiteLLM), M2d (bootstrap questionnaire).
