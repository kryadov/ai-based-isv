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

## 11. Окружение стенда (директива владельца, 2026-11-14)

Живой стенд работает на **встроенном Kubernetes Rancher Desktop** (k3s, движок moby) на Windows-машине — НЕ создавать k3d-кластер и НЕ поднимать отдельный WSL-дистрибутив. Все команды бутстрапа/флоу/гейтов — через toolbox-контейнер из Git Bash: `./infra/scripts/toolbox.sh <cmd>` (выставляет `SWARM_CLUSTER=rancher-desktop`). Подробности — INSTALL.ru.md «Windows (Rancher Desktop + toolbox)».

**LLM (директива владельца, 2026-11-15):** на стенде нет GPU — никакого локального инференса и Ollama. LiteLLM маршрутизирует все алиасы (`qwen3` — агентная модель с tool calls, `qwen-coder` — лёгкая модель, `reviewer` — бот-ревьюер) в **z.ai GLM Coding Plan**. **OpenRouter используется ТОЛЬКО когда исчерпан лимит z.ai** (z.ai отвечает HTTP 429; после этого он «остывает» 5 минут, и трафик идёт через запасные алиасы OpenRouter). Любая другая ошибка z.ai (5xx, таймаут, 400, авторизация) повторяется на z.ai и возвращается вызывающему — в OpenRouter не уходит. Штатные `fallbacks` LiteLLM срабатывают на любую ошибку, поэтому коллбэк прокси `infra/litellm/zai_limit_gate.py` выключает их для каждого запроса до этого 429; не добавляйте алиас, который ходит в OpenRouter напрямую (`infra/litellm/deployment.yaml`, проверяется `apps/bootstrap/tests/test_litellm_manifest.py`). Локальные секреты — в **`.env`** в корне репо (в .gitignore; шаблон `.env.example`: `ZAI_API_KEY=`, `OPENROUTER_API_KEY=`); каждый запуск секции `litellm` записывает заданные там ключи в OpenBao `secret/litellm/{zai,openrouter}`, откуда они идут в Secret кластера (`bootstrap.sh llm-key [zai|openrouter]` — импорт без передеплоя). Ключи не коммитить и не выводить в логи/чат — проверять наличие, а не значение. После перезапуска Rancher Desktop OpenBao поднимается запечатанным: сначала `./infra/scripts/toolbox.sh bash infra/scripts/bootstrap.sh vault` (идемпотентный unseal), потом остальные секции.

## 12. Самообеспечение: платформа разрабатывает себя (спека §13.7, уровень 2)

Платформа развивается через собственный коридор. Канонический репозиторий — GitHub `kryadov/ai-based-isv`; рабочая копия, куда коммитят агенты, — `swarm-lab/ai-based-isv` в лабораторном Gitea.

1. `bash infra/scripts/platform-sync.sh pull` (Git Bash на хосте) — fast-forward `main` в Gitea из GitHub; отказывается, пока в Gitea есть непромоутнутые коммиты.
2. Задача — issue в `swarm-lab/ai-based-isv`; запуск: `./infra/scripts/toolbox.sh bash infra/tests/selfdev_flow.sh` (текст задачи — через переменные `SELFDEV_*`).
3. Агент работает от НЕ-админского `swarm-agent`; PR проходит профиль проверок платформы (`.gitea/workflows/ci.yml` — те же гейты, что в GitHub CI, плюс gitleaks/semgrep), бот-ревью и раунды исправлений.
4. **HITL на каждом мерже** — человек (или оператор с полномочиями владельца) читает дифф и одобряет выведенный HITL-запрос; автомерж для этого репо выключен (`platform_repos`, `.platform/merge-policy.yaml`).
5. `bash infra/scripts/platform-sync.sh promote` → GitHub PR `selfdev/<sha7>` → зелёный GitHub CI → мерж (никогда не прямой push в `main` GitHub) → `platform-sync.sh pull`.

Агенты никогда не получают админ-токен Gitea, а `.gitea/**` и `.platform/**` — защищённые пути гейтов: изменения самого коридора делает пара человек+агент через PR на GitHub.
