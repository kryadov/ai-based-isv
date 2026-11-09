# AI-Native ISV — Engineering Guidelines for AI Agents

> Русский вариант оригинала: [AGENTS.ru.md](AGENTS.ru.md)

## 1. Главный принцип: методология Superpowers
Каждый агент обязан действовать по Superpowers (`.agents/skills/`, `.agents/rules/superpowers.md`): сначала скиллы (`brainstorming` до дизайна, `writing-plans`/`executing-plans` для многошаговых задач, `test-driven-development` — RED-GREEN-REFACTOR обязателен, `systematic-debugging` для багов, `verification-before-completion` перед «готово»); качество важнее скорости, аннотации и тесты обязательны.

## 2. Технологии — это решения, а не аксиомы
Стек платформы зафиксирован в ADR и пересматривается через ADR: `docs/adr/0001-platform-stack.md` (Python ≥3.12, uv-workspace, pydantic v2, mypy --strict, ruff, pytest, FastAPI, Postgres+pgvector, DBOS, FastMCP), `0002-executor-opencode.md` (Executor: OpenCode в k3s-песочнице; PydanticAI для лёгких reasoning-ролей), `0003-memory.md` (память проекта и организации).

## 3. Стандарты кода платформы
Python ≥3.12, src-layout, hatchling; mypy --strict без подавлений; ruff; pytest; TDD обязателен.

## 4. Стандарты целевых проектов (полиглот)
Целевые проекты не подчиняются разделу 3: их стандарты приходят с check-профилем стека из шаблона бутстреппера (спека §7.2.4); LLM-проекты — eval-сьюты как тесты, версии промптов в манифесте; существующий трекер — через TaskSource-адаптер.

## 5. Структура репозитория
`.agents/` (скиллы Superpowers) · `.github/workflows/` (CI) · `docs/` (adr, superpowers: specs/plans/research, architecture) · `packages/` (isv_core, agent_runtime, context_engine, mcp_servers, evals) · `apps/` (bootstrap, agent_api, isv_product) · `infra/` (L0 как код: OpenBao, Gitea, OpenProject, k3s+Argo, LiteLLM).

## 6. Human-in-the-Loop
Деструктивные действия (деплой в prod, миграции, прямые push в main, права/секреты) — только с явным подтверждением человека.

## 7. Локализация
Код, комментарии и основная документация — только на английском; русский — исключительно в `*.ru.md` файлах.

## 8. Git-дисциплина — уроки инцидентов
1. Никогда `git add -A` — только явные пути; артефакты (agent-api.db, input-docs/, .vault-root-token, unseal keys) в .gitignore до старта работы.
2. Никаких cherry-pick между ветками; перенос работы — merge (PR-flow), мелкие доки — прямой коммит на main.
3. История переписывается только до публикации (filter-branch для секретов/артефактов).
4. Не запускать бутстраппер/скрипты в параллельных экземплярах (гонка опустошает секреты).
5. Бинарные артефакты (тестовые БД, PDF) не в git.
6. Перед git-операцией: `git status` + `git branch --show-current`; переходы — через checkout.
7. Интеграции — только через merge.

## 9. Выполнение и проверка
Перед «готово» — вывод команд: `uv run ruff check .`, `uv run mypy packages apps`, `uv run pytest`.

## 10. Карта проекта
`STATE.md` — состояние и следующий шаг (обновлять после каждого инкремента) · `ARCH.md`/`ARCH.ru.md` — архитектура со схемами · `TODO.md` — бэклог · спека + аддендумы — полный дизайн.
