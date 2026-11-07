# M1: Монорепо-каркас — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Рабочий uv-workspace монорепо (5 пакетов + 3 приложения, зелёная тройка верификации, CI на GitHub) + AGENTS.md v2 + ADR-0001..0003.

**Architecture:** uv-workspace с src-layout в каждом члене; корневой pyproject держит общие конфиги ruff/mypy(strict)/pytest; приложения — FastAPI с `/healthz`; библиотеки — минимальные пакеты с `__version__` и `py.typed`. CI — GitHub Actions, те же три команды, что локально (один коридор, спека §7.2.4).

**Tech Stack:** Python >=3.12, uv (workspaces), pydantic v2 (транзитивно через fastapi позже; в M1 — только инструментальная тройка), ruff, mypy --strict, pytest, FastAPI, hatchling.

**Spec:** `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md` (§3 ограничения, §6.1, §7.1 структура, §7.8 стек, §10.1 M1)

## Global Constraints

- Python `>=3.12`; целевая версия интерпретатора `3.12`.
- Команды верификации дословно (AGENTS.md): `uv run ruff check .` → 0 ошибок; `uv run mypy packages apps` → 0 ошибок (`strict = true`); `uv run pytest` → все зелёные.
- Версии всех новых пакетов: `0.1.0`; `requires-python = ">=3.12"` в каждом члене.
- Build backend всех членов: hatchling; layout: `src/<имя_пакета>/`.
- Ruff: `target-version = "py312"`, `line-length = 100`.
- Имена членов workspace: `isv_core`, `agent_runtime`, `context_engine`, `mcp_servers`, `evals`, `bootstrap`, `agent_api`, `isv_product` (спека §7.1).
- Никаких доменных моделей в M1 — только каркас и smoke-уровень (домен — M3).
- Каждый шаг с изменениями кода заканчивается коммитом; финальный push — только в Task 8.

## Review Focus

1. Чистый клон: `uv sync --all-packages` + тройка верификации должны быть зелёными без ручных шагов — pinned: Task 4 (локально) и Task 5 (CI).
2. src-layout под `mypy packages apps`: mypy должен резолвить модули как `isv_core` и т.д., а не `src.isv_core` — pinned: `mypy_path` со всеми 7 src-путями в Task 1, проверка в Task 4.
3. `TestClient` требует httpx — pinned: httpx в корневой dev-группе (Task 1), тест `/healthz` в Task 3.
4. Отсутствие `py.typed` сломает strict-mypy у будущих потребителей пакетов — pinned: тест на наличие маркера в каждом пакете (Task 2).
5. Несовпадение имени члена workspace и имени пакета ломает `uv sync --all-packages` — pinned: sync в Task 4 и CI (Task 5).

---

### Task 1: Корневой workspace и общие конфиги

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Modify: `.gitignore` (убедиться: `.venv/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `uv.lock` — НЕ игнорируется)

**Interfaces:**
- Produces: корневые конфиги, от которых зависят все следующие задачи; члены workspace по маскам `packages/*`, `apps/*`; dev-группа `dev` (ruff, mypy, pytest, httpx).

- [ ] **Step 1: Создать `pyproject.toml` с точным содержимым**

```toml
[project]
name = "ai-based-isv"
version = "0.1.0"
description = "AI-native ISV: swarm agent development platform (org bootstrap + swarm orchestration + delivery)"
requires-python = ">=3.12"

[dependency-groups]
dev = [
    "ruff>=0.9",
    "mypy>=1.14",
    "pytest>=8.3",
    "httpx>=0.27",
]

[tool.uv.workspace]
members = ["packages/*", "apps/*"]

[tool.ruff]
target-version = "py312"
line-length = 100
src = ["packages", "apps"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.12"
strict = true
explicit_package_bases = true
mypy_path = [
    "packages/isv_core/src",
    "packages/agent_runtime/src",
    "packages/context_engine/src",
    "packages/mcp_servers/src",
    "packages/evals/src",
    "apps/bootstrap/src",
    "apps/agent_api/src",
    "apps/isv_product/src",
]

[tool.pytest.ini_options]
testpaths = ["packages", "apps"]
addopts = "-q"
```

- [ ] **Step 2: Создать `.python-version` с содержимым `3.12`**
- [ ] **Step 3: Проверить/дополнить `.gitignore`** записями: `.venv/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`. `uv.lock` коммитится (не добавлять в игнор).
- [ ] **Step 4: Верифицировать установку**

Run: `uv sync` (workspace пуст — должен пройти без членов; члены добавят следующие задачи)
Expected: создан `.venv`, установлен `uv.lock`, группа dev на месте.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .python-version .gitignore uv.lock
git commit -m "chore(m1): root uv workspace with ruff/mypy-strict/pytest configs"
```

### Task 2: Пять библиотечных пакетов (скелет + smoke)

**Files (одинаковый шаблон ×5, имена ниже):**
- Create: `packages/<name>/pyproject.toml`, `packages/<name>/src/<pkg>/__init__.py`, `packages/<name>/src/<pkg>/py.typed`, `packages/<name>/tests/test_smoke.py`
- Пары dir→pkg: `isv_core`→`isv_core`, `agent_runtime`→`agent_runtime`, `context_engine`→`context_engine`, `mcp_servers`→`mcp_servers`, `evals`→`evals`

**Interfaces:**
- Produces: импортируемые пакеты с `__version__: str = "0.1.0"` и маркером `py.typed`; зависимости `[]`.

- [ ] **Step 1: Для каждого из 5 пакетов — pyproject (пример для isv_core; остальные идентичны с заменой имён и description)**

```toml
[project]
name = "isv_core"
version = "0.1.0"
description = "Domain models and event contracts for the swarm development platform"
requires-python = ">=3.12"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/isv_core"]
```

Descriptions: `agent_runtime` — "Agent harness: Executor interface, OpenCode adapter, sandbox runtime, session logs"; `context_engine` — "Codebase indexing and project/organization memory banks"; `mcp_servers` — "MCP tool servers: task-source, git, ci, deploy/observability, memory, llm"; `evals` — "Spec self-evals and agent evaluation benchmarks".

- [ ] **Step 2: `__init__.py` каждого пакета**

```python
"""<однострочное описание из pyproject>."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Пустой файл `py.typed` в каждом `src/<pkg>/`**
- [ ] **Step 4: Smoke-тест (пример для isv_core; остальные идентичны с заменой импорта) — пишется ДО проверки, что зелёный**

```python
from pathlib import Path

import isv_core


def test_package_imports_and_version() -> None:
    assert isinstance(isv_core.__version__, str)


def test_py_typed_marker_present() -> None:
    assert (Path(isv_core.__file__).parent / "py.typed").exists()
```

- [ ] **Step 5: Верифицировать**

Run: `uv sync --all-packages && uv run pytest packages/isv_core/tests -v`
Expected: 10 тестов PASS (2 × 5 пакетов).

- [ ] **Step 6: Commit**

```bash
git add packages
git commit -m "feat(m1): library packages skeleton (isv_core, agent_runtime, context_engine, mcp_servers, evals)"
```

### Task 3: Приложения bootstrap / agent_api / isv_product (FastAPI /healthz)

**Files:**
- Create: `apps/agent_api/pyproject.toml`, `apps/agent_api/src/agent_api/__init__.py`, `apps/agent_api/src/agent_api/main.py`, `apps/agent_api/tests/test_health.py`
- Create: `apps/isv_product/pyproject.toml`, `apps/isv_product/src/isv_product/__init__.py`, `apps/isv_product/src/isv_product/main.py`, `apps/isv_product/tests/test_health.py`
- Create: `apps/bootstrap/pyproject.toml`, `apps/bootstrap/src/bootstrap/__init__.py`, `apps/bootstrap/tests/test_smoke.py`

**Interfaces:**
- Consumes: шаблон из Task 2 (pyproject/build).
- Produces: `agent_api.main.create_app() -> FastAPI` и `isv_product.main.create_app() -> FastAPI` с `GET /healthz → {"status": "ok"}`; пакет `bootstrap` с `__version__`.

- [ ] **Step 1: pyproject agent_api** — как в Task 2, но:

```toml
dependencies = ["fastapi>=0.115", "uvicorn>=0.34"]
```
`packages = ["src/agent_api"]`, description = "Swarm control plane API: task queue, leases, HITL inbox, audit".

- [ ] **Step 2: `apps/agent_api/src/agent_api/main.py`**

```python
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="agent_api")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
```

`__init__.py` — как Task 2 (версия 0.1.0).

- [ ] **Step 3: `apps/agent_api/tests/test_health.py`**

```python
from fastapi.testclient import TestClient

from agent_api.main import create_app


def test_healthz() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 4: Повторить Steps 1–3 для `isv_product`** (title `"isv_product"`, description = "Demo service seeded by the organization bootstrapper", packages = ["src/isv_product"]).
- [ ] **Step 5: `apps/bootstrap`** — pyproject как в Task 2 (`packages = ["src/bootstrap"]`, description = "Interactive organization bootstrapper: questionnaire, L0 provisioning, project repo seeding"), `__init__.py` с `__version__`, smoke-тест как в Task 2 Step 4 (импорт `bootstrap`, версия, py.typed).
- [ ] **Step 6: Верифицировать**

Run: `uv sync --all-packages && uv run pytest apps -v`
Expected: 4 теста PASS (agent_api healthz + isv_product healthz + bootstrap ×2), 0 FAIL.

- [ ] **Step 7: Commit**

```bash
git add apps
git commit -m "feat(m1): app skeletons — bootstrap, agent_api, isv_product with /healthz"
```

### Task 4: Тройка верификации зелёная локально

**Files:** Modify при необходимости: `pyproject.toml` (только конфиг-фиксы, если верификация вскрыла проблемы)

**Interfaces:**
- Consumes: всё из Tasks 1–3.

- [ ] **Step 1:** Run: `uv run ruff check .` — Expected: «All checks passed!» (иначе чинить импорты/стиль по выводу, не отключая правила).
- [ ] **Step 2:** Run: `uv run mypy packages apps` — Expected: «no issues found» (иначе чинить аннотации; `strict = true` не ослаблять).
- [ ] **Step 3:** Run: `uv run pytest` — Expected: все тесты зелёные, 0 FAIL.
- [ ] **Step 4: Commit (если были фиксы)**

```bash
git add -A && git commit -m "fix(m1): verification trio green"
```

### Task 5: CI на GitHub Actions

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: корневой pyproject и члены (Tasks 1–3); те же команды, что Task 4.

- [ ] **Step 1: `.github/workflows/ci.yml` с точным содержимым**

```yaml
name: ci
on:
  push:
    branches: [main]
  pull_request:
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
      - run: uv python install 3.12
      - run: uv sync --all-packages
      - run: uv run ruff check .
      - run: uv run mypy packages apps
      - run: uv run pytest
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci(m1): verification trio on GitHub Actions"
```

### Task 6: AGENTS.md v2

**Files:**
- Modify: `AGENTS.md` (полная замена)

**Interfaces:**
- Consumes: спека (пути разделов), ADR-файлы из Task 7 (ссылки по именам до их создания — Task 7 идёт сразу следом в том же пуше).

- [ ] **Step 1: Заменить AGENTS.md содержимым ниже (точная копия)**

````markdown
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
````

- [ ] **Step 2: Commit**

```bash
git add AGENTS.md
git commit -m "docs(m1): AGENTS.md v2 — methodology mandate, tech decisions delegated to ADRs"
```

### Task 7: ADR-0001, ADR-0002, ADR-0003

**Files:**
- Create: `docs/adr/0001-platform-stack.md`, `docs/adr/0002-executor-opencode.md`, `docs/adr/0003-memory.md`

**Interfaces:**
- Consumes: спека §7.8 (стек, решено), research-deepdive §2 (ресёрч-обоснование).

- [ ] **Step 1: `docs/adr/0001-platform-stack.md`** — статус Accepted, 2026-11-07. Контекст: платформа ~70% AI-инженерии + ~30% plumbing; мандат типизации. Решение (verbatim из спеки §7.8, строки «Control plane», «Durable execution», «MCP-серверы»): Python ≥3.12 + uv-workspace; pydantic v2 + mypy --strict; ruff; pytest; FastAPI + Postgres (asyncpg/SQLAlchemy); DBOS (`pydantic-ai[dbos]`); FastMCP (streamable HTTP); pgvector. Альтернативы: TS-ядро (Mastra/Vercel AI SDK — MCP reference impl, но теряем PydanticAI durable maturity; один язык с будущим React-панелем), Go-ядро (один бинарник, но слабейшая AI/MCP-экосистема), полиглот-ядро (3× операционная нагрузка для v0). Последствия: mypy strict как барьер качества; uv.lock как источник воспроизводимости. Условия пересмотра: >40% усилий уходит в plumbing; появляется TS-фреймворк с сопоставимым durable execution — новый ADR.
- [ ] **Step 2: `docs/adr/0002-executor-opencode.md`** — статус Accepted, 2026-11-07. Контекст: coding-вершины требуют зрелый harness (тулы редактирования/bash-политика/permissions), который своя петля не воспроизведёт (research-deepdive §2.2). Решение: интерфейс `Executor` (start/deliverable/done_when/paths_allow → события/дифф/статус); первый адаптер — OpenCode (`opencode serve`, REST/SSE) в поде k3s; permission allow/deny по умолчанию, `ask` — мост в HITL-инбокс; тонкий клиент генерируется из `/openapi.json`; pin версии образа `ghcr.io/anomalyco/opencode`; storage изолирован на воркер (`OPENCODE_DB`/volume). Альтернативы: своя петля PydanticAI (для coding слабее), Codex SDK (запасной адаптер №2), Claude Agent SDK (vendor-lock), AG2/CrewAI backbone (недетерминированный control flow, O(N·M²) токены). PydanticAI остаётся для лёгких ролей. Условия пересмотра: нестабильность OpenCode API (V2→V3 ломает контракт) → второй адаптер; потребность в мульти-harness → расширение `Executor`.
- [ ] **Step 3: `docs/adr/0003-memory.md`** — статус Accepted, 2026-11-07. Контекст: проекты несвязанные/связанные, нужен org-уровень (спека §7.4). Решение: проектный semantic — в самом репо (AGENTS.md/.specs, harness читает нативно); project working/episodic — Postgres + pgvector; org semantic — git-репо через PR; memory-extractor (PydanticAI) пишет episodic после вершины; консолидация на закрытии Run → promotion PR (ревью человек/champion); правило «опубликован ≠ улучшает результат» (eval на исторических задачах); промоция в v0 — только человек-автор, авто после ~10 dogfood-задач. Альтернативы: единый векторный store (Letta-стиль — теряем git-аудит semantic), всё в LLM-контексте (не масштабируется). Условия пересмотра: retrieval-качество < порога на dogfood-корпусе; появление встроенных memory-провайдеров с PR-flow.
- [ ] **Step 4: Commit**

```bash
git add docs/adr
git commit -m "docs(m1): ADR-0001 platform stack, ADR-0002 executor opencode, ADR-0003 memory"
```

### Task 8: Финальная верификация и push

- [ ] **Step 1:** Run: `uv run ruff check . && uv run mypy packages apps && uv run pytest` — Expected: все три зелёные, вывод показать.
- [ ] **Step 2:** Run: `git push origin main` — Expected: push успешен.
- [ ] **Step 3:** Проверить CI: `gh run watch` (или открыть Actions) — Expected: workflow `ci` зелёный на main. Если красный — чинить до зелёного (это и есть приёмка M1).
