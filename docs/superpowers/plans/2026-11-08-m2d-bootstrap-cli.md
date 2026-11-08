# M2d: apps/bootstrap — вопросник + идемпотентное применение + засев demo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (native — устоявшийся выбор владельца).

**Goal:** CLI `bootstrap`: вопросник (цель, трекер, LLM, demo) → профиль `infra/env.local.yaml` → идемпотентное применение секций L0 → засев demo-репо в Gitea → env-манифест. Приёмка: CLI на живом стенде идемпотентно проходит все секции и засевает demo-репо (API-проверка).

**Architecture:** typer-CLI поверх существующих секций `infra/scripts/bootstrap.sh` (subprocess, вывод стримится); профиль — `infra/env.local.yaml` (gitignored); засев — Gitea API токеном из OpenBao. Детектор prerequisites (docker/k3d/helm/kubectl/uv/gh/ollama).

**Spec:** `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md` §10 M2d; addendum §6.1

## Global Constraints

- Python ≥3.12, typer, mypy --strict, ruff; не хранить секреты в профиле.
- Вопросы: (1) цель: ноутбук/k3d [default]; (2) трекер: deploy OpenProject | connect existing (URL/токен — v0: только OpenProject, existing — заглушка-подсказка); (3) LLM: host Ollama [default] | внешний ключ (v0: заглушка); (4) demo-репо: yes [default].
- Приёмка (критерий спеки §10 M2d): CLI non-interactive на живом стенде → все секции зелёные, demo-репо в Gitea существует, env.yaml консистентен.

## Tasks

1. **T1 Skeleton + prereq-check**: пакет, typer app, `check` команда (детект инструментов, таблица ok/NO), smoke-тест.
2. **T2 Questionnaire + профиль**: `init` команда (typer prompts с дефолтами) → `infra/env.local.yaml`; smoke-тест генерации.
3. **T3 Apply**: `apply` команда — последовательный вызов секций (subprocess, стрим вывода, stop-on-error), `--section` для отдельных, `--yes` (non-interactive для acceptance).
4. **T4 Demo seeding**: `seed-demo` — создать Gitea repo `isv-product` (токен из OpenBao), push начального README; идемпотентно.
5. **T5 Acceptance**: `infra/tests/m2d_flow.sh` — CLI `init --defaults` + `apply` + `seed-demo` + проверки; INSTALL.md финализировать (раздел L0 Bootstrap → реальная команда CLI); trio; PR.

## Review Focus

1. CLI не должен хранить/выводить секреты (токен читается из OpenBao в момент seed).
2. Повторный `apply` — идемпотентен (секции устойчивы — проверено в M2a-c).
3. Прерывание (Ctrl-C) посреди apply не оставляет полусостояний хуже, чем «секция не применена» (секции атомарны сами по себе).
