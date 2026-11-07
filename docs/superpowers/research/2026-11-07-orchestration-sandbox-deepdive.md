# Дип-ресёрч: оркестрация агентов и песочницы (ноябрь 2026)

Статус: вход в выбор архитектуры исполнения (подаёт на ADR-0002/0003 после брейншторма).
Источники: три параллельных веб-ресёрча (OpenCode SDK/state; harness SDK конкурентов; конверсационные фреймворки + песочницы). Ссылки в тексте.

## 1. Развёртка задачи: три разных слоя, которые называют одним словом «фреймворк»

| Слой | Что решает | Кто даёт готовое |
|---|---|---|
| **Control plane (оркестрация)** | кто/что/когда: очередь вершин, lease, граф зависимостей, merge queue, backpressure, HITL-инбокс, бюджеты, audit | **никто** — всегда свой (или Temporal-класс движок под низ) |
| **Модель исполнения агента** | как агент думает и действует | (а) своя петля на библиотеке (PydanticAI), (б) **harness SDK готового кодинг-агента** (OpenCode / Claude Agent SDK / Codex / Goose), (в) конверсационные фреймворки (AG2 / CrewAI / MS Agent Framework), (г) граф-рантаймы (LangGraph) |
| **Песочница** | где исполняются «руки» | Docker → gVisor/Kata microVM → SaaS (E2B/Daytona/Modal) → k8s agent-sandbox |

Наша спека (§7.3) уже проектирует control plane сами; вопрос этого ресёрча — слои 2 и 3.

## 2. Модель исполнения: три класса кандидатов

### 2.1 Своя петля на PydanticAI (текущее предложение спеки)
Пишем цикл сами: tools через MCP-toolsets, structured outputs, DBOS durable execution, бюджет-метеринг.
- Плюсы: полный контроль над loop-политикой; идеальная интеграция с нашим VET-манифестом и session log; типизация.
- Минусы: **мы переизобретаем кодинг-harness** — инструменты редактирования, поиск, bash-политика, permission-гейты, LSP. У зрелых harness'ов (Claude Code/OpenCode/Codex) это годы итераций. Для CODING-вершин наша петля будет объективно слабее.

### 2.2 Harness SDK готового кодинг-агента (программируемый исполнитель)
Воркер платформы управляет готовым агентом по API: создал сессию → отправил структурированный промпт вершины → стримит события → забрал дифф/коммиты/аудит.

| Harness | Провайдеры | HITL (внешние аппрувы) | Аудит | Песочница | Зрелость/риски |
|---|---|---|---|---|---|
| **OpenCode** (MIT, anomalyco) | ✅ нативно любой OpenAI-compatible → LiteLLM | permission rules allow/deny/**ask**; в headless `ask` отвечает клиент через `POST /session/:id/permissions/:id`; org-level policies (hard-deny) | SSE-шина (tool calls c input/output, диффы, `session.idle`), REST `GET /session/:id/message` (все parts), `/diff`, storage в SQLite (`OPENCODE_DB` → можно вынести на volume), плагин-хуки `tool.execute.before/after` для экспорта в наш audit | реальная изоляция = контейнер (shell с правами хост-юзера); внутри — ограничение путей (`external_directory`), парсер shell-команд | V1→V2 сломал API; Python SDK alpha (V1-генерация) → свой тонкий клиент из `/openapi.json`; зависания старта в слабых контейнерах (#43465); ask в headless висит без ответа (#14473) — настраивать allow/deny; общий `$HOME` у нескольких serve теряет сессии (#30302) — изолировать storage на воркер |
| **Codex SDK / app-server** (Apache-2.0) | ✅✅ лучший: OpenAI/Ollama/Bedrock/Vertex + любой OpenAI-compatible (`model_providers.*`, wire_api) | JSON-RPC `item/permissions/requestApproval` → внешняя система; `approval_policy`; `auto_review` guardian-модель | rollout JSONL, `thread/items/list`, OTel (`codex.tool_decision`, `tool_result`), `notify` webhook | OS-уровень: Seatbelt (macOS) / Landlock+seccomp (Linux); read-only/workspace-write/danger-full-access; сеть opt-in; критичные пути (.git) read-only | repo не принимает PR; cloud закрыт; зрелый |
| **Claude Agent SDK** (Commercial ToS) | ❌ только Claude (обход: LiteLLM Anthropic-compatible, Bedrock/Vertex/Foundry) | ★★★ эталон: 6-ступенчатая оценка, `canUseTool` callback — блокировка до решения внешней системы | JSONL-транскрипты, `session_store`-адаптер (зеркало в наше хранилище), hooks, OTel, file checkpointing | встроенный sandboxed bash (Seatbelt/bubblewrap) + сетевой прокси с allowlist и маскированием кредов; devcontainer/VM/Managed Agents | vendor-lock на Claude; SDK Python/TS первоклассные |
| **Goose** (Apache-2.0, LF) | ✅✅✅ 40+ провайдеров, LiteLLM, ACP-провайдеры | ACP `session/requestPermission` (stdsubprocess или serve HTTP/WS) | сессии/resume, стрим tool-calls; хуки ограничены | «своими руками» — контейнер на стороне платформы | зрелость ниже big-tech harness |
| Gemini CLI / Cursor CLI | vendor-locked | внешних гейтов нет / --yolo | JSONL-стримы | есть (Gemini rich) / нет (Cursor) | не кандидаты на роль основного исполнителя |

### 2.3 Конверсационные фреймворки (AG2 / CrewAI / MS Agent Framework) — «агенты общаются на уровне API»
- **AG2**: ConversableAgent + GroupChatManager; реплика выбранного спикера бродкастится всем в общий транскрипт; manager выбирает спикера LLM-вызовом. Нет durable workflow, типизация на `**kwargs` (mypy --strict не пройти), `autogen-agentchat` в maintenance.
- **CrewAI**: crews (role/goal/backstory, sequential/hierarchical process — hierarchical = manager-LLM делегирует, риск бесконечного делегирования) и **flows** (событийный typed workflow: `@start/@listen/@router`, pydantic-состояние, checkpointing, `@human_feedback` = готовые approval-gates, LiteLLM из коробки).
- **MS Agent Framework** (GA 04.2026): Pregel-подобные superstep-workflows, чекпоинт на каждом шаге, resume на другой машине, HITL first-class; Python-релизы ломаются между версиями.
- **Почему они не backbone для нашего конвейера** (класс задачи — детерминированная доставка кода с аудитом):
  1. LLM-управляемый control flow (выбор спикера, делегирование) — недетерминирован, невоспроизводим, слабо аудируется — противоречит нашему принципу «детерминированные стадии» (Паттерн 4).
  2. Токен-стоимость: group chat держит общий транскрипт — рост ~O(N×M²); AG2 FAQ сам признаёт «hard to control if the number of agents is large».
  3. Нет lease-очередей, песочниц, git-интеграции, audit trail как артефакта — всё равно строим сами (наш control plane).
  - **Где они уместны**: вложенный шаг внутри этапа (обсуждение спеки планировщиком и специалистами), CrewAI Flows / MAF Workflows — как референс для нашего vertex-движка.

**Вывод слоя 2**: для CODING-вершин harness SDK даёт зрелый кодинг-харнесс, который мы не построим на PydanticAI. Для легковесных reasoning-ролей (self-eval спеки, бот-ревьюер, judge, консолидация памяти) своя типизированная петля (PydanticAI) — дешевле и структурнее, чем гонять полный harness. Конверсационные — не backbone.

## 3. Песочницы

| Уровень | Механика | Изоляция | Задержка старта | Когда |
|---|---|---|---|---|
| Hardened Docker | non-root, egress-allowlist, read-only/clone mount, seccomp, секреты по краткосрочным токенам (не env) | общее ядро — НЕ security boundary; bind-mount — главная дыра (CVE-2026-14891, CVE-2026-27002) | ~1-3 c | **v0-лаборатория** (доверенная нагрузка) |
| **k8s agent-sandbox** (SIG, CRD Sandbox/SandboxWarmPool) | pre-warmed поды, managed NetworkPolicy (default-deny, блок RFC1918/metadata), RuntimeClass на выбор | gVisor (userspace-ядро; используется в code execution у OpenAI и Anthropic) или Kata (microVM в поде; AWS Lambda/Fly) | суб-секундная из warm pool | **production-этап** платформы |
| SaaS: E2B / Daytona / Modal | Firecracker microVM на сессию, pause/resume, snapshots | microVM | <100 мс | быстрый выход на рынок; E2B self-host свёлся к enterprise; биллинг wall-clock (HITL-паузы тарифицируются!) |
| macOS хостинг | OrbStack (~85% нативной fs, проприетарный) / Colima / Rancher Desktop | = Docker в VM | — | dev-машины; Apple Container ещё не готов (сеть ×5) |

Правило индустрии: **egress-политика и scoped-креды первичны, рантайм вторичен**. Docker Sandboxes `sbx` (clone mode: репо read-only + приватная RW-клон) — готовый референс паттерна «worktree-песочница».

## 4. Рекомендуемая архитектура (гибрид) — как это работает

```
Control plane (наш, FastAPI+Postgres): Run → вершины → lease → gates → merge queue
   │ воркер берёт вершину
   ▼
Песочница (контейнер): worktree + harness (OpenCode serve / Codex app-server)
   │ наш воркер: POST /session → structured prompt вершины (deliverable, done_when, paths_allow)
   │ SSE-стрим событий → наш session log (NDJSON) — audit в реальном времени
   │ permission:ask → мостик в HITL-инбокс control plane
   ▼
done_when-валидация (наш код, не LLM) → коммит/PR + evidence pack
```

- **Coding-вершины**: harness SDK (вопрос — какой: §5).
- **Лёгкие роли** (self-eval, бот-ревью, judge, консолидация памяти, планирование): PydanticAI — structured outputs, без песочницы.
- **Durability**: контроль состояния вершины — в control plane; session log — у нас (стрим из harness); упавший контейнер = retry вершины (книга: «увязшую итерацию выгоднее перезапустить на чистом входе»); опционально — вынос storage harness'а на volume (`OPENCODE_DB`) для resume сессии.
- **VET-манифест** пополняется полями harness: версия образа, версия harness, конфиг permissions, провайдер/resolved_model.

## 5. Вилки для брейншторма (решаются по одной)

1. **Модель исполнения coding-вершин**: (A) harness SDK — рекомендую; (B) своя петля PydanticAI + MCP-тулы; (C) конверсационный фреймворк как backbone — ресёрч против.
2. **Какой harness**: OpenCode (MIT, провайдер-агностичен, AGENTS.md-нативен; сырой Python SDK — свой клиент по OpenAPI) vs Codex (зрелый JSON-RPC, провайдер-агностичен; Apache-2.0, но нет PR в repo) vs Claude Agent SDK (эталонный HITL/аудит; lock на Claude). Возможен адаптер поверх ACP (agentclientprotocol.com) или мульти-harness через наш интерфейс `Executor`.
3. **Уровень песочницы v0**: hardened Docker (рекомендую) vs k8s pods сразу vs microVM/SaaS.
4. **Судьба PydanticAI**: остаётся для лёгких ролей (рекомендую) vs уходим целиком в harness+свои скрипты.
