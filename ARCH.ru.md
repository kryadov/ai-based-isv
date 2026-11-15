# ARCH — как работает AI-Организация (схемы)

> Русская версия: [ARCH.md](ARCH.md) (EN)

Архитектурная правда — в спеке `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md`
(§7 платформа, §13 жизненный цикл проектов). Этот файл — визуальная карта; обновляется
с каждым инкрементом.

## 1. Общая картина — что делает организация

Цель организации: **разработка, внедрение и поддержка проектов ПО** (продукты,
внутренние сервисы, тулы, ассистенты) — работу выполняют рои AI-агентов, люди держат
гейты-утверждения.

```mermaid
flowchart LR
    TRK["Трекер: задача создана] --> API[Control plane\nagent_api"]
    API --> SPEC[Планировщик\nстадии спеки + self-eval]
    SPEC -->|HITL-гейт 1: аппрув плана| HUMAN((Человек))
    HUMAN --> SW[Swarm-исполнение:\nагенты-исполнители в песочницах]
    SW --> PR[Pull Request\n+ evidence pack]
    PR --> CI[CI-гейты: тесты, security, сканы]
    CI --> BR[Бот-ревью]
    BR -->|HITL-гейт 2: мерж| HUMAN
    HUMAN --> CD[Argo Rollouts\nКанарейка 5-25-50-100%]
    CD -->|анализ OK| PROD((Production))
    CD -->|деградация| RB[Авто-откат]
    MEM[("Память организации:\nуроки, reuse, конституция")] -.-> SPEC
    MEM -.-> SW
    SW -->|write-back| MEM
```

## 2. Топология L0-стенда (ноутбук, k3d/k3s — работает сейчас)

Всё разворачивается с нуля: `uv run org-bootstrap apply` (идемпотентно).

```mermaid
flowchart TB
    subgraph k3d["k3d-кластер swarm-lab (k3s v1.35)"]
        subgraph platform["namespace: platform"]
            OB["OpenBao\nraft на PVC\nauto-init/unseal"]
            PG["Postgres 17 + pgvector\nCNPG-оператор\nбазы: gitea, openproject, litellm, swarm"]
            GT["Gitea\ngit + PR + issues + OCI-реестр\nNodePort 30080"]
            RUN["act_runner\ndind, PVC-идентичность"]
            AR["Argo CD\nNodePort 30081"]
            KS["kube-prometheus-stack\nGrafana 30082 / Prometheus"]
            LK["Loki + Alloy\ndaemonset: логи подов"]
            OP["OpenProject\nтрекер UI 30083\nauth сломан — см. TODO"]
            LL["LiteLLM-шлюз\n30084 → z.ai / OpenRouter"]
        end
        subgraph prod["namespace: prod"]
            PODINFO["podinfo demo\nGitOps-sync, debug-логи"]
        end
        AG["agent-воркеры\n(M3+: claim вершин)"]
    end
    ZAI["(z.ai GLM Coding Plan\nqwen3 → glm-5.3\nqwen-coder → glm-5.3-flash)"]
    ORT["(OpenRouter API — только при лимите z.ai\nglm-5.3 / deepseek-v4.1-flash / kimi)"]
    LL -->|основной, ключи из OpenBao| ZAI
    LL -.->|при лимите плана 429| ORT
    GH["GitHub (public)\nканонический репо платформы + CI"] -.-> AR
    TRK(("Трекер\n(задача на входе)")) -.-> API
```

Порты: Gitea 30080, Argo CD 30081, Grafana 30082, OpenProject 30083, LiteLLM 30084.

## 3. Конвейер доставки — одна задача от входа до prod

```mermaid
sequenceDiagram
    participant T as Трекер (issue)
    participant API as Control plane
    participant P as Планировщик
    participant H as Человек
    participant X as Исполнитель (песочница)
    participant G as Gitea
    participant C as CI + бот-ревью
    participant D as Argo Rollouts

    T->>API: webhook/poll: задача готова
    P->>P: стадии спеки (requirements→design→tasks)
    P->>P: spec self-eval (PASS/FAIL/WEAK)
    P->>H: HITL-гейт 1 — аппрув плана
    H->>API: approve
    API->>X: claim вершины (lease, paths_allow, done_when)
    X->>X: worktree + session log + метеринг бюджета
    X->>G: коммит + PR + evidence pack
    G->>C: CI-гейты (тесты/security/сканы)
    C->>C: проход бот-ревью
    C->>H: HITL-гейт 2 — человеческий мерж
    H->>G: merge
    G->>D: сборка образа + канарейка 5→100%
    D-->>API: маркер успеха / авто-откат
    API->>API: audit-журнал + write-back памяти
```

Принцип масштабирования (спека §8): узкое место — **пропускная способность проверки**
(гейты/ревью), а не генерации. Merge queue + backpressure ограничивают рой скоростью
интеграции.

**Реализовано с M5 (`agent_runtime/gates.py`, проверено `m5_flow.sh`):**
- После пуша `agent/vertex-N` воркер ведёт коридор:
  PR → CI → бот-ревью → раунды исправлений (≤2) → HITL-мерж (или авто-мерж по политике, если меняются только docs) → мерж.
  В трекер на каждом этапе уходит комментарий `[platform]`.
- CI — это **check-профиль** репо, workflow Gitea Actions, который раскладывает бутстрапер:
  - задача `checks`: ruff, mypy, pytest;
  - задача `security`: gitleaks, semgrep.
  `main` защищён обеими проверками.
- Ревьюер (алиас LiteLLM `reviewer`) работает на z.ai `glm-5.3`, как и всё остальное — OpenRouter только при исчерпании лимита z.ai (директива владельца); запасной на время лимита — модель другого семейства (`moonshotai/kimi-k3.1-pro`). Пока работаем на z.ai, независимость ревью держится на промпте ревьюера, а не на семействе модели.
- Любое неизвестное или сбойное состояние блокирует мерж. Агент никогда не меняет `.gitea/**` и `.platform/**`.

```mermaid
flowchart LR
    V["вершина готова<br/>ветка запушена"] --> PR["PR (переиспользуется<br/>между попытками)"]
    PR --> CI{"CI checks +<br/>security"}
    CI --> RV{"бот-ревью<br/>(другое семейство)"}
    RV -->|"красный CI или блокирующие"| FX["раунд исправлений ≤2<br/>(агент на ветке PR)"]
    FX --> CI
    RV -->|"зелёно и чисто"| POL{"политика мержа"}
    POL -->|"только docs"| M["мерж"]
    POL -->|"иначе"| HITL["HITL-подтверждение<br/>мержа"]
    HITL --> M
    M --> DONE["трекер Done<br/>+ ссылка на PR"]
```

### 3b. Канареечный выкат (M6)

```mermaid
flowchart LR
    M["мерж в Gitea"] --> B["деплоер: сборка isv-product:sha7<br/>+ базовая линия"]
    B --> H{"первый выкат или<br/>изменение deploy/**?"}
    H -->|"да"| HITL["HITL #3"]
    H -->|"нет"| P
    HITL --> P["Argo CD Application<br/>image override"]
    P --> R["Argo Rollouts canary<br/>20 → 50 → 100 %"]
    R --> A{"анализ: канарейка vs стабильная<br/>+ окно после выката<br/>(Prometheus, k6 traffic)"}
    A -->|"прошёл"| OK["успех:<br/>вердикт в evidence + трекер"]
    A -->|"провал"| RB["прерывание → откат на стабильную<br/>+ postmortem.md"]
```

### 3a. Цикл самообеспечения (спека §13.7, уровень 2)

Репо самой платформы — тоже целевой проект. Канон — GitHub; рабочая копия агентов — Gitea `swarm-lab/ai-based-isv`, синхронизация — `infra/scripts/platform-sync.sh`. Агенты работают от не-админского `swarm-agent`; каждый мерж требует HITL (для `platform_repos` автомержа нет).

```mermaid
flowchart LR
    GH["GitHub main<br/>(канон)"] -->|"platform-sync pull<br/>(только fast-forward)"| GT["Gitea swarm-lab/ai-based-isv<br/>main"]
    GT --> TASK["issue → run → vertex<br/>(swarm-agent, песочница)"]
    TASK --> COR["PR → CI платформы → ревью<br/>→ HITL → мерж"]
    COR --> GT
    GT -->|"platform-sync promote"| PRG["GitHub PR selfdev/&lt;sha7&gt;"]
    PRG -->|"зелёный GitHub CI → мерж"| GH
```

## 4. Модель сворм-исполнения

- **Один писатель на участок кода**; специалисты ревьюят, не пишут совместно.
- Роли: **координатор** (план/judge/правила памяти) → **специалисты** (архитектура,
  security, тесты) → **исполнители** (по вершине, изолированный worktree + под, MCP-тулы).
- Разделение **brain / hands / session** (Паттерн 19): петля — мозг, песочница — руки,
  а append-only NDJSON **session log** переживает сбои — воркер может умереть, вершина
  переигрывается/возвращается с сохранёнными доказательствами.
- Контракт вершины: `{id, deps, paths_allow, paths_avoid, done_when, deliverable}`.
  `done_when` проверяется **кодом**, а не отказом LLM failing.

### 4·M8. Реализованный режим роя (v0.9.0)

```mermaid
flowchart LR
    T["задача (issue)"] --> PL["swarm plan<br/>LLM-координатор → проверенный граф"]
    PL --> Q["очередь с lease<br/>(deps, захват SKIP LOCKED)"]
    Q --> W1["воркер w1"] & W2["воркер w2"] & W3["воркер w3"]
    W1 & W2 & W3 --> C["коридор на каждую вершину<br/>PR → CI → ревью → HITL → мерж"]
    C -->|"результат = handoff"| Q
    C --> J["swarm judge<br/>всё done + main проходит"]
```

- **Планировщик** (`agent_runtime.planner`): LLM предлагает `{name, deps, paths_allow}`, платформа проверяет граф до запуска. Проверки:
  - имена уникальны, зависимости известны, циклов нет, вершин не больше 5;
  - пути внутри разрешённой области задачи;
  - **вершины, которые могут идти одновременно, не делят файлы**.

  `done_when` задаёт платформа, команд от планировщика нет. При любом сбое — откат к одной вершине.
- **Параллельные исполнители** (`swarm run --workers N`): N процессов-воркеров на один run, у каждого своя lease-идентичность (`<agent>-w<i>`) и рабочий каталог. У каждой вершины свой под-песочница и NodePort.
- **Handoff:** в промпт зависимой вершины попадает «Handoff from upstream vertices» — файлы, PR и ревью каждой зависимости. Ответ `claim` содержит `deps`, граф отдаёт `GET /runs/{id}/vertices`.
- **Backpressure:** воркер не берёт новую вершину, пока HITL ждут не меньше `max_pending_merges` мержей (по умолчанию 3). Ожидание аудитится как `backpressure`.
- **Judge** (`swarm judge`): все вершины `done`, и смерженный `main` проходит done-команду run. Тогда run получает статус `done`, а в память проекта пишется эпизод `decision`.
- **Метрики:** `GET /metrics` control plane отдаёт `swarm_vertices{status}`, `swarm_hitl_pending{kind}` и `swarm_runs{status}`.

### 4·M9. Полиглотный коридор

Всё, что зависит от стека, — это данные:
- check-профили в `infra/templates/check-profiles/<stack>`: `python`, `typescript`, `docs`;
- шаблоны кода в `infra/templates/projects/<name>`: `ts-service`.

Коридор, агенты, ревьюер, HITL и память от стека не зависят. `m9_flow.sh` доказывает это на TypeScript-сервисе. Приёмочная команда для чужого стека запускает его инструменты в контейнере (например, `node:22` через docker), поэтому рантайм языка на хосте не нужен.

### 4·M10. Панель организации и автоматизации

- **Панель** `/ui` (agent_api, ES-модули без зависимостей): вход только для людей (подписанные сессии, локальные пользователи; OIDC позже). Вкладки: Входящие, Проекты, Рой, Автоматизации, Метрики и Память, переключатель EN/RU. Каждое действие идёт через `/ui/api` и аудитится под именем человека.
- **Автоматизации:**
  - определения — это код (`infra/automations/*.yaml`);
  - прогоны хранит control plane (`automation_runs`);
  - движок на хосте (`agent_runtime.automations`) выполняет работу и создаёт карточку во «Входящих» (HITL `automation`);
  - **действия — только после одобрения человеком**: для `vuln-scan` это по одной задаче в трекере на каждый затронутый репо.

### 4a. Каталог ролей агентов

Реализуется через реестр `identities` (M3): роль маппится на `allowed_sections`
(gitea, ci, memory, litellm, deploy...) и на то, к какой стадии конвейера агент допущен.
Люди имеют отдельные аккаунты только с HITL-правами.

| Роль | Тип | Ответственность | Стадия конвейера | Доступ |
|---|---|---|---|---|
| **planner** | специалист | приём задачи → стадии спеки, уточняющие вопросы, lookup reuse, посев задач | до кода | трекер, память |
| **architect** | специалист | ревью спеки/дизайна, инварианты, предложения reuse, проверки дрейфа | спека + ревью | git read, память |
| **developer** | исполнитель | реализация одной вершины (код + локальные тесты), обработка замечаний бот-ревью | исполнение (open-code harness) | git, gitea, ci, litellm |
| **tester** | специалист/исполнитель | написание/поддержка тестов, flaky-hunter, покрытие, приёмка release-кандидата | исполнение + релиз | git, ci, evals |
| **reviewer (бот)** | лёгкий агент | первый проход код-ревью на PR, дедуп находок, severity-метки | гейты | gitea, ci read |
| **security** | специалист | ревью SAST/зависимости/секреты, threat modeling, аудит безопасного харнесса | гейты + automations | ci read, сканеры |
| **judge** | лёгкий агент | решение о завершении: проверка evidence по `done_when`, sanity session log | по вершине + руну | память read |
| **support** | исполнитель | тикеты поддержки, черновики пост-мортемов, воспроизведение регрессий | цикл поддержки | трекер, git, наблюдаемость |
| **maintenance (janitor)** | исполнитель | свипы: мёртвый код, осиротевшие тесты, дрейф док/OpenAPI, hygiene памяти | automations | git, ci |
| **release** | исполнитель | сборка дистрибутивов: архив, Docker-образы, checksums+SBOM (внешние проекты, §13.5) | канал релизов | gitea registry, ci |

Automations переиспользуют эти роли с триггерами (vuln-scan → security; dep-updates →
developer; log-analysis → support; feedback-ingest → planner). Люди: владелец (HITL,
промоция памяти), чемпион (ревью конфигурации проекта), дежурный (алерты канарейки).

## 5. Память (спека §7.4, ADR-0003)

```mermaid
flowchart TB
    subgraph project["Память проекта"]
        W["working: состояние текущей задачи"]
        E["episodic: что пробовали/не вышло"]
        S1["semantic: in-repo AGENTS.md/.specs"]
    end
    subgraph org["Память организации (git-репо, PR-flow)"]
        CON["конституция: инварианты, политики"]
        RC["reuse-каталог: компоненты + версии"]
        LE["уроки: дистилляция инцидентов/эпизодов"]
    end
    E -->|консолидация + HITL промоция PR| LE
    LE -.->|срез retrieval на старте задачи| W
    CON -.->|progressive disclosure| W
```

Права записи по ролям; изменения semantic — только через PR; «опубликован» ≠
«улучшает результат» (eval-гейт). TTL/консолидация защищают от дрейфа и отравления.


**Сделано в M7 (v0.8.0):**
- **Эпизодическое хранилище:** таблица `memory_episodes` в БД control plane, API `/memory/episodes`, `/memory/slice`.
  - Ранжирование: пересечение путей +3, общие слова задачи до +5, последние 7 дней +1, postmortem/failure +2.
  - Эпизод без пересечения путей и слов не возвращается никогда.
- **Запись** воркером при закрытии вершины, fail-open:
  - `deploy` (маркер успеха) или `postmortem` (откат);
  - `failure` (терминальный провал, исчерпанные попытки);
  - `lesson` от **memory-extractor** (лёгкая модель, не больше 2 на задачу).
- **Retrieval на старте вершины:** срез попадает в промпт агента («Project memory (lessons from earlier tasks — follow them)»), а его id — в session log (`memory_slice`).
- **Org-память:** репо Gitea `swarm-lab/org-memory` с `lessons/` и `reuse-catalog.yaml` (reuse-каталог v1), под check-профилем docs.
  - `org-bootstrap memory promote --episode N` открывает PR на ревью человеку.
  - Агенты в org-память не пишут.
- **Отложено** (TODO): working-память (M8), эмбеддинги, индексация кода, self-evals спек, автопромоция.

## 6. Внутренности control plane (текущее состояние, M3)

`agent_api` (FastAPI + SQLAlchemy async):

- **runs / vertices** — граф задач, атомарный claim с lease TTL + heartbeat, возврат
  при сбое (`UPDATE ... RETURNING`, SKIP LOCKED на Postgres).
- **identities** — сервисные идентичности агентов (scoped, заменяемые).
- **budgets** — расход токенов по субъекту, атомарное ограничение (`spent + X <= limit`).
- **HITL-инбокс** — карточки решений; одно решение на запрос (409 при повторе).
- **audit** — append-only журнал событий (actor/action/target/outcome/payload).
- **merge queue** — записи для сериализации интеграции (гейтинг — M5).

Эндпоинты: `/runs`, `/runs/{id}/vertices`, `/vertices/claim|heartbeat|complete|fail`,
`/identities`, `/budgets/{subject}[/consume]`, `/hitl[/pending|/decision]`, `/audit`,
`/merge-queue`, `/healthz`.

## 7. Модель исполнителя (M4, дизайн)

OpenCode-харнесс работает **внутри пода k3s** (initContainer-клон, NetworkPolicy
allow-list только к LiteLLM/MCP/Gitea, краткосрочный Vault-токен). Воркер управляет им
через REST/SSE: структурированный промпт вершины на вход, поток событий → session log,
дифф → PR. `done_when` валидируется нашим кодом. Permission `ask` мостится в HITL-инбокс.
Тот же swarm-движок гоняет **Automations** (сканы, обновления зависимостей, анализ
логов, приём фидбека) — результаты попадают карточками в Inbox.

## 8. Карта верификации

```bash
uv run ruff check . && uv run mypy packages apps && uv run pytest   # код платформы
./infra/tests/m2a_pr_flow.sh        # Gitea PR-flow + branch protection
./infra/tests/m2b_deploy_flow.sh    # GitOps → prod + метрики + логи
./infra/tests/m2c_flow.sh           # задачи трекера → poller + LLM-шлюз
./infra/tests/m2d_flow.sh           # бутстреппер-CLI end-to-end
./infra/tests/m3_flow.sh            # control plane на живом Postgres (M3)
./infra/tests/m4a_flow.sh           # M4a: вершина-исполнитель, opencode serve в docker → session log + manifest + ветка в Gitea
./infra/tests/m4b_flow.sh           # M4b: вершина в k3s-поде-песочнице: NetworkPolicy + Secret на прогон + NodePort, Postgres закрыт
./infra/tests/m4c_flow.sh           # M4c: полный цикл — issue в Gitea → поллер через task-source MCP (дедуп) → run → агент в k3s с MCP (tracker, llm) → ветка → issue закрыт с вердиктом
```

Перекрёстные ссылки: полный дизайн §7.2 (конвейер), §7.3 (оркестрация), §7.4 (память),
§7.7 (HITL), §13 (жизненный цикл проектов); аддендум UI/Automations 2026-11-08.
