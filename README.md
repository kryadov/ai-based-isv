# AI-Native ISV — Swarm Agent Development Platform

Внутренняя платформа агентской разработки: **вход** — задача в трекере, **выход** — изменение в production (тесты всех уровней, security, code review, канареечный выкат). Сворм-оркестрация агентов (координаторы → специалисты → исполнители в изолированных песочницах), память проекта и организации.

## Документация

| Файл | Что это |
|---|---|
| [INSTALL.md](INSTALL.md) | Установка: prerequisites, dev setup, L0 bootstrap |
| [AGENTS.md](AGENTS.md) | Правила работы агентов (Superpowers — мандат) |
| [docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md](docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md) | Полный дизайн платформы |
| [docs/adr/](docs/adr/) | Архитектурные решения (стек, Executor, память) |
| [docs/superpowers/plans/](docs/superpowers/plans/) | Планы инкрементов (M1, M2a, …) |
| [docs/superpowers/research/](docs/superpowers/research/) | Ресёрч (оркестрация, песочницы) |

## Слои

- **L0** — инфраструктура организации как код (`infra/`): Gitea, OpenProject, Vault, Postgres, k3s+Argo, LiteLLM, наблюдаемость. Разворачивается `infra/bootstrap.sh` на чистой машине.
- **L1** — платформа (`packages/`, `apps/agent_api`): swarm-оркестратор, память, quality gates, evidence pack.
- **L2** — сквозная доставка (`apps/isv_product` — demo-проект).

## Быстрые команды

```bash
uv sync --all-packages      # установка окружения
uv run ruff check .         # линт
uv run mypy packages apps   # строгая типизация
uv run pytest               # тесты
```

Статус инкрементов: M1 ✓ (каркас) · M2a в работе (L0-ядро: k3d + Vault + Postgres + Gitea).
