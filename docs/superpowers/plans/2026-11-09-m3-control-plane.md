# M3: isv_core + control plane (agent_api) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (native — выбор владельца).

**Goal:** Доменные модели платформы + control plane API: очередь вершин с lease, идентичности агентов, бюджеты, HITL-инбокс, merge queue, audit-журнал.

**Architecture:** isv_core — pydantic v2 модели и енумы (чистые, без БД); agent_api — FastAPI + SQLAlchemy 2 async (asyncpg на Postgres); таблицы через `metadata.create_all` на старте (Alembic — когда схема стабилизируется, Ruling в плане); claim вершины — `UPDATE ... WHERE status='queued' ... RETURNING` c `with_for_update(skip_locked=True)` (на Postgres — SKIP LOCKED; на SQLite в тестах игнорируется). Интеграционный тест против живого CNPG Postgres — opt-in через env.

**Tech Stack:** pydantic v2, SQLAlchemy 2 async, asyncpg, aiosqlite (тесты), FastAPI, pytest + pytest-asyncio/anyio.

**Spec:** §7.1 (структура), §7.3 (orchestration), §7.7 (HITL), §10.3 (M3)

## Global Constraints

- trio-дисциплина: ruff/mypy --strict/pytest зелёные после каждой задачи.
- isv_core не зависит от БД/FastAPI (чистые модели).
- agent_api DB URL из env `AGENT_API_DATABASE_URL` (дефолт sqlite+aiosqlite для юнитов); интеграция с живым Postgres — env `M3_INTEGRATION_PG_URL` (opt-in).
- Тесты: каждая функция store/API покрыта тестом; RED→GREEN.

## Tasks

1. **isv_core.domain**: енумы (RunStatus, VertexStatus, LeaseState, HITLStatus, DecisionAction) + модели (TaskVertex: id/run_id/name/deps/paths_allow/done_when/status/leased_by/lease_until/attempts; Run: id/status/budget_tokens/budget_spent; HITLRequest/Decision; AuditEvent; AgentIdentity; MergeRequest entry) + round-trip тесты. Commit.
2. **agent_api skeleton**: settings (DB URL), FastAPI factory + /healthz, lifespan create_all, deps (session factory). SQLite-тесты API. Commit.
3. **Store: vertices + lease**: таблицы runs/vertices; store-функции create_run/add_vertex/claim_vertex (FOR UPDATE SKIP LOCKED)/heartbeat/complete/fail(requeue + attempts++). Тесты на sqlite: claim единственной queued, повторный claim не выдаёт leased, requeue после fail. Commit.
4. **Identities + budgets**: таблицы identities/budgets; register/list identities; check_and_consume_budget (отказ при исчерпании). Тесты. Commit.
5. **HITL inbox**: таблица hitl_requests; create/list/pending/approve/reject (с decision_by/причиной); только pending → решается. Тесты. Commit.
6. **Audit journal**: таблица audit_events (append-only, ts, actor, action, target, payload json); append/list c фильтром. Тесты. Commit.
7. **API endpoints**: runs (POST/GET), vertices (claim для воркера: POST /vertices/claim?agent=..., POST /vertices/{id}/heartbeat|complete|fail), identities, budgets, hitl (POST create/decision, GET pending), audit GET, merge queue (POST enqueue/list). Интеграционный тест против живого Postgres (opt-in env). Commit.
8. **Acceptance m3_flow.sh**: против живого CNPG (db swarm через CNPG Database CR + bootstrap section_cnpg) — полный цикл: run → vertices → claim → heartbeat → complete → HITL approve → audit; плюс trio + m2a-d не сломаны; PR → merge.

## Review Focus

1. Двойной claim: две «сессии» claim не должны получить одну вершину (транзакция + статусный фильтр).
2. TTL lease: heartbeat продлевает; умерший воркер → вершина возвращается в очередь (проверка временем lease_until в claim-запросе).
3. HITL: double-approve → 409.
4. Audit: append-only (нет update/delete путей).
5. Бюджет: списание сверх остатка запрещено (concurrent-safe: UPDATE ... WHERE spent+X <= limit RETURNING).
