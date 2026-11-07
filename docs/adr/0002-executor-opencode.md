# ADR-0002: Executor — OpenCode harness

Статус: Accepted · Дата: 2026-11-07 · Драйверы: спека §7.8, research-deepdive §2.2, решение владельца (fork №1: A + OpenCode)

## Контекст

Coding-вершины требуют зрелый кодинг-harness (тулы редактирования, поиск, bash-политика, permissions), который собственная петля на PydanticAI не воспроизведёт — у готовых harness'ов это годы итераций.

## Решение

- Контракт `Executor` в `agent_runtime`: start(deliverable, done_when, paths_allow) → стрим событий, дифф, статус. Control plane знает только интерфейс.
- Первый адаптер — **OpenCode** (MIT): `opencode serve` (REST + SSE) в поде k3s.
- Permission-политика: allow/deny по умолчанию; `ask` — мост в HITL-инбокс control plane (в headless неотвеченный `ask` зависает — issue #14473).
- Тонкий Python-клиент генерируется из `/openapi.json` (официальный Python SDK — alpha и генерирован по V1 API).
- Pin версии образа `ghcr.io/anomalyco/opencode`; storage изолирован на воркер (`OPENCODE_DB`/volume) — несколько serve с общим `$HOME` теряют сессии (#30302).
- VET-манифест пополняется полями harness: версия образа, конфиг permissions, resolved_model.
- **PydanticAI остаётся** для лёгких reasoning-ролей: spec self-eval, бот-ревьюер, judge, memory-extractor, консолидация памяти (structured outputs, без песочницы).

## Альтернативы

- Своя петля на PydanticAI для coding-вершин — полный контроль, но слабейший харнесс.
- Codex SDK/app-server (Apache-2.0, зрелый JSON-RPC, провайдер-агностичен) — запасной адаптер №2.
- Claude Agent SDK — эталонный HITL/аудит, но lock на Claude-модели (Commercial ToS).
- AG2/CrewAI как backbone — отклонены: LLM-управляемый control flow недетерминирован, общий транскрипт растёт ~O(N·M²), lease/gates/аудит всё равно строить самим.

## Последствия

Зависимость от внешнего продукта и его версии (управляется pin'ом и интерфейсом `Executor`); события SSE стримятся в наш session log — audit не зависит от хранилища harness.

## Условия пересмотра

Нестабильность OpenCode API (V2→V3 ломает контракт) или потребность в мульти-harness — расширение `Executor` вторым адаптером (Codex первым кандидатом).
