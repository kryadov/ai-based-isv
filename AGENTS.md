# AI-Native ISV — Engineering Guidelines for AI Agents

Репозиторий платформы агентской разработки (сворм-оркестрация + бутстреп организации + сквозная доставка). Полный дизайн: `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md`.

## 1. Prime Directive: Superpowers Methodology

Каждый агент в этом репозитории обязан следовать фреймворку Superpowers (`.agents/skills/`, `.agents/rules/superpowers.md`):

1. **Сначала скиллы**: `brainstorming` — до любых новых фич/спек/дизайнов; `writing-plans` / `executing-plans` — для многошаговых задач; `test-driven-development` — RED-GREEN-REFACTOR обязателен; `systematic-debugging` — для багов; `verification-before-completion` — перед любым «готово».
2. **Качество важнее скорости**: никакого спекулятивного/непротестированного кода; задачи bite-sized; не отключать линтеры/типизатор/тесты ради «зелёного» вида.

## 2. Технологии — это решения, не аксиомы

Технологический стек платформы зафиксирован в ADR и пересматривается через ADR, не правкой этого файла:

- `docs/adr/0001-platform-stack.md` — Python ≥3.12, uv-workspace, pydantic v2, mypy --strict, ruff, pytest, FastAPI, Postgres+pgvector, DBOS, FastMCP.
- `docs/adr/0002-executor-opencode.md` — Executor: OpenCode harness в k3s-песочнице; PydanticAI — для лёгких reasoning-ролей.
- `docs/adr/0003-memory.md` — память проекта и организации.

## 3. Стандарты кода ПЛАТФОРМЫ (этот репозиторий)

- Python `>=3.12`, src-layout, hatchling; версии пакетов — pydantic-совместимый стиль семвера.
- Типизация: `mypy --strict`, ноль подавлений без ADR-уровневого обоснования.
- Стиль: `ruff` (check + format).
- Тесты: pytest; TDD обязателен для любого кода.

## 4. Стандарты ЦЕЛЕВЫХ ПРОЕКТОВ (полиглот)

Целевые проекты (Java/Python/TS/React, ассистенты, тулы) НЕ подчиняются разделу 3. Их стандарты приходят с check-профилем стека, который бутстреппер зашивает в шаблон репо (спека §7.2.4). Для LLM-проектов/ассистентов eval-сьюты — тесты первого класса, версии промптов — в манифесте прогона. Существующий трекер подключается через `TaskSource`-адаптер (спека §6.1, §9).

## 5. Структура репозитория

```
├── .agents/                 # скиллы и правила Superpowers
├── .github/workflows/       # CI платформы (GitHub — канонический репо, спека §6.1)
├── docs/
│   ├── adr/                 # архитектурные решения (технологии живут здесь)
│   ├── superpowers/         # specs/, plans/, research/
│   └── architecture/        # диаграммы и доменные модели
├── packages/
│   ├── isv_core/            # доменные модели и контракты событий
│   ├── agent_runtime/       # Executor (OpenCode), песочницы k3s, session logs
│   ├── context_engine/      # индексация кода, память проекта и организации
│   ├── mcp_servers/         # MCP: task-source, git, ci, deploy/observability, memory, llm
│   └── evals/               # spec self-evals, агентские бенчмарки
├── apps/
│   ├── bootstrap/           # бутстреппер организации (вопросник, L0, засев проектов)
│   ├── agent_api/           # control plane: очередь, lease, HITL-инбокс, аудит
│   └── isv_product/         # demo-проект бутстрепа
└── infra/                   # L0 как код (M2): Vault, Gitea, OpenProject, k3s+Argo, LiteLLM
```

## 6. Human-in-the-Loop

Деструктивные действия (деплой в prod, миграции БД, прямые push в main, права/секреты) — только с явным подтверждением человека (спека §7.2.4, §7.7).

## 7. Verification Commands

Перед отчётом «готово» выполнить и показать вывод (evidence before assertions):

```bash
uv run ruff check .
uv run mypy packages apps
uv run pytest
```
