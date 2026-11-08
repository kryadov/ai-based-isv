# M2c: OpenProject + LiteLLM + TaskSource-поллер — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (native — устоявшийся выбор владельца). Шаги с чекбоксами.

**Goal:** Трекер задач (OpenProject, self-hosted) и шлюз моделей (LiteLLM над локальной Ollama) на k3d; TaskSource-поллер доставляет изменения задач в тестовый endpoint. Приёмка: задача создаётся через API → поллер доставляет изменение → chat completion через шлюз возвращает ответ → вызов виден в логе.

**Architecture:** OpenProject — helm chart с внешним Postgres (CNPG Cluster, отдельная Database CR); LiteLLM — Deployment с ConfigMap-конфигом (модель Ollama по OpenAI-compatible endpoint хоста), NodePort 30084; TaskSource-поллер (M2c-версия) — CronJob/скрипт, полный MCP task-source — в M4. Ollama на хосте (Metal-ускорение), LiteLLM ходит на `host.lima.internal:11434`.

**Tech Stack:** charts.openproject.org (OpenProject), CNPG Database CR, ghcr.io/berriai/litellm (пин), ollama (brew, host), python http.server для тест-endpoint.

**Spec:** `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md` (§6, §10 M2c), аддендум §2 (automations: log-analysis и др. требуют шлюз)

## Global Constraints

- namespace `platform`; NodePort OpenProject UI `30083`, LiteLLM `30084`.
- Версии чартов/образов → `infra/env.yaml`; `latest` запрещён.
- Секреты (пароль админа OP, LiteLLM master key) → Vault; в git не попадают.
- Идемпотентность bootstrap-секций (helm upgrade --install + wait).
- Webhooks OpenProject — Enterprise-фича: TaskSource v0 = **polling** `/api/v3/work_packages?updated_at` (Ruling зафиксировать в ledger; webhook-путь — при появлении Enterprise).
- Каждый инкремент обновляет md-доки (требование владельца).

## Review Focus

1. OpenProject на внешнем CNPG: pods Ready только когда БД реально доступна; повторный bootstrap не теряет данные проекта (PVC/БД).
2. LiteLLM master key не в git; вызов без ключа → 401.
3. Поллер: доставляет изменение ровно один раз при повторном прогоне (дедуп по updated_at) — проверяется двойным прогоном в Task 6.
4. Ollama на хосте: LiteLLM в k3d ходит на `host.lima.internal:11434` — при недоступности Ollama LiteLLM отвечает 5xx с внятной ошибкой (не висит).
5. arm64: все образы (openproject, litellm) multi-arch; проверка статусов подов.

---

### Task 1: Ветка + исследование чарта OpenProject

- [ ] Step 1: ветка `m2c-trackers-gateway`; ledger.
- [ ] Step 2: `helm repo add openproject https://charts.openproject.org`, `helm show values openproject/openproject` — зафиксировать: ключи external postgres, админ-ENV, ingress/service, resource defaults. Записать вывод в ledger.
- [ ] Step 3: Ruling по конфигурации (external CNPG vs chart-postgres; админ через ENV) — в ledger.

### Task 2: Postgres для OpenProject (CNPG Database CR)

**Files:** Create `infra/postgres/openproject-database.yaml`; Modify bootstrap.sh (section_cnpg дополнить apply Database CR)

- [ ] Step 1: Database CR `openproject` (owner openproject) в Cluster postgres-platform.
- [ ] Step 2: apply + проверка `psql -l` видит базу; secret `openproject-app` создан CNPG.
- [ ] Step 3: Commit `feat(m2c): openproject database on cnpg`

### Task 3: OpenProject helm install

**Files:** Create `infra/openproject/values.yaml`; Modify bootstrap.sh (section_openproject)

- [ ] Step 1: values.yaml — external postgres (host/user/password из CNPG-секрета через --set), admin ENV (логин/пароль генерятся → Vault), service NodePort 30083, resources скромные.
- [ ] Step 2: bootstrap section_openproject: helm upgrade --install --wait; wait pods; curl UI 200; API v3 basic-auth `/api/v3/status` → 200.
- [ ] Step 3: пароль админа + endpoint → Vault `secret/openproject/admin`.
- [ ] Step 4: Commit `feat(m2c): openproject on k3d with external cnpg`

### Task 4: Ollama на хосте + модель

**Files:** Modify INSTALL.md (prerequisites: ollama + модель), bootstrap.sh section_models (только проверка доступности хост-сервиса из кластера)

- [ ] Step 1: `brew install ollama`, `brew services start ollama`, `ollama pull qwen2.5-coder:7b`.
- [ ] Step 2: проверка из пода: `kubectl run` curl `http://host.lima.internal:11434/api/tags` → 200 и модель в списке (Ruling: colima → host.lima.internal; для Docker Desktop — `host.docker.internal`; определить по context).
- [ ] Step 3: INSTALL обновить. Commit `feat(m2c): host ollama as model backend`

### Task 5: LiteLLM gateway

**Files:** Create `infra/litellm/config.yaml`, `infra/litellk`→`infra/litellm/deployment.yaml` (опечатку не допускать); Modify bootstrap.sh (section_litellm)

- [ ] Step 1: ConfigMap litellm config: model `qwen-coder` → openai-compatible `http://host.lima.internal:11434/v1`; general_settings master_key из Vault (`secret/litellm/master`).
- [ ] Step 2: Deployment + Service NodePort 30084; probes.
- [ ] Step 3: bootstrap section_litellm: master key → Vault; apply; wait; проверка: `curl POST /v1/chat/completions` с ключом → 200 и непустой content; без ключа → 401.
- [ ] Step 4: Commit `feat(m2c): litellm gateway over host ollama`

### Task 6: TaskSource-поллер → тест endpoint

**Files:** Create `infra/tests/task_source_poller.py` (или scripts), `infra/tests/m2c_flow.sh`; Modify INSTALL

- [ ] Step 1: тест-endpoint: python http.server, принимает POST, сохраняет последнюю полезную нагрузку в файл.
- [ ] Step 2: поллер: OP API v3 basic-auth → GET work_packages (updated_after=последний чекпоинт) → POST в endpoint; чекпоинт-файл (gitignored).
- [ ] Step 3: m2c_flow.sh: (a) создать work package через API; (b) поднять тест-endpoint; (c) запустить поллер 1 цикл; (d) assert payload содержит id/тазвание; (e) второй прогон поллера — пустой payload (дедуп); (f) chat completion через LiteLLM → content непуст; (g) litellm pod log содержит запрос. PASS/FAIL вывод.
- [ ] Step 4: Commit `test(m2c): tasksource polling + gateway acceptance`

### Task 7: Финал

- [ ] Step 1: trio + все acceptance (m2a, m2b, m2c) зелёные; env.yaml обновлён (openproject/litellm/ollama).
- [ ] Step 2: INSTALL финал (доступ OP, LiteLLM API пример, ollama prereq); README.ru синхрон.
- [ ] Step 3: PR `m2c-trackers-gateway` → CI → merge → финальный отчёт (evidence + rulings).
