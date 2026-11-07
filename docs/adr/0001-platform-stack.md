# ADR-0001: Стек платформы

Статус: Accepted · Дата: 2026-11-07 · Драйверы: спека §7.8, research-deepdive (docs/superpowers/research/2026-11-07-orchestration-sandbox-deepdive.md)

## Контекст

Платформа — ~70% AI-инженерии (агентные петли, evals, память, retrieval) и ~30% plumbing (очередь, API, deploy-клей). Мандат организации: строгая типизация и воспроизводимость. Целевые проекты полиглотны и не зависят от языка платформы.

## Решение

- Python ≥3.12, uv-workspace монорепо (src-layout, hatchling, uv.lock как источник воспроизводимости).
- pydantic v2 + `mypy --strict` (ноль подавлений без ADR-уровневого обоснования); ruff (lint+format); pytest.
- Control plane: FastAPI + Postgres (asyncpg/SQLAlchemy); очередь вершин — `SELECT … FOR UPDATE SKIP LOCKED` + lease/heartbeat.
- Durable execution: DBOS (`pydantic-ai[dbos]`) поверх Postgres — без отдельного сервера; апгрейд-путь — Temporal (нативно поддержан PydanticAI).
- MCP-серверы: FastMCP / официальный `mcp` SDK, транспорт streamable HTTP.
- Память: Postgres + pgvector для retrieval; semantic — git-репозитории через PR (ADR-0003).

## Альтернативы

- **TypeScript-ядро** (Mastra / Vercel AI SDK): референсная реализация MCP — TS, один язык с будущей React-панелью; но теряем зрелость PydanticAI durable execution и FastMCP-экосистему, runtime-валидация через zod вне мандата pydantic.
- **Go-ядро**: один статический бинарник и дешёвый параллелизм, но слабейшая AI/агентная экосистема под нашу специфику.
- **Полиглот-ядро** (control plane Go + агенты Python): честно для большой организации, 3× операционная нагрузка для лаборатории v0 — против принципа «соразмерно задаче».

## Последствия

mypy --strict — барьер качества каждого PR; единая среда исполнения упрощает VET-манифесты (один runtime, один набор версий).

## Условия пересмотра

>40% усилий уходит в plumbing; появляется TS/Go-фреймворк с сопоставимым durable execution и типизацией — новый ADR, не правка этого.
