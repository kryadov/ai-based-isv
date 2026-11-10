# M4: agent_runtime — Executor/OpenCode + первые MCP — Design

Дата: 2026-11-09 · Статус: ready for owner approval · Основания: спека §7.3/§7.8/§10.3 (M4), ADR-0002, STATE.md (M3 закрыт, release v0.3.0) · Builder check: 2026-11-09, вопросы закрыты (см. «Решения builder check»)

## Intent и почему сейчас

Control plane (M3) умеет выдавать вершины под lease, но исполнителей нет: claim-ить некому.
M4 замыкает первый полный цикл платформы: **задача в трекере → агент кодит в песочнице → PR с evidence**.
Это первый момент, когда платформа «разрабатывает ПО агентами» на реальном harness, а не заглушках.

## Goals / Non-goals / Anti-goals

**Goals (M4 целиком, тремя инкрементами):**
1. Контракт `Executor` в `agent_runtime` + адаптер OpenCode (тонкий клиент по `/openapi.json`, pin версии образа).
2. Session log (append-only NDJSON), манифест прогона, done_when-валидация кодом, paths_allow-проверка.
3. Песочница в k3s: pod с initContainer-клоном, NetworkPolicy allow-list, короткоживущий Vault-токен, PVC.
4. Воркер: claim вершины из `agent_api` → запуск Executor → heartbeat → complete/fail с evidence.
5. MCP-серверы (FastMCP, streamable HTTP): task-source, git, llm — подключены к OpenCode-сессиям.
6. E2E-приёмка: Gitea issue → run → агент в поде → ветка → PR с evidence; статусы задачи меняются через task-source.

**Non-goals (в другие里程碑):** PydanticAI+DBOS для лёгких ролей → **M5** (решение владельца, 2026-11-09); HITL-мост для permission `ask` — после M5 (в v0 — только явные allow/deny); гейты/бот-ревью (M5), canary (M6), memory (M7), swarm-координация (M8), UI (M10).

**Anti-goals (так не делаем):** done_when не валидируется LLM — только кодом; `ask`-разрешения в headless (зависает, issue #14473); kubectl port-forward к живым сервисам (NodePort — урок M3); `git add -A`-стиль в скриптах (AGENTS.md §9).

## Constraints

- Ноутбук-стенд: k3d 6CPU/24GB, LLM — локальный Ollama (qwen2.5-coder:7b) через LiteLLM :30084. Работа агента будет медленной (минуты на вершину) — это норма, приёмка не должна требовать скорости.
- mypy --strict, ruff, pytest — зелёные после каждой задачи (trio-дисциплина AGENTS.md).
- Образ `ghcr.io/anomalyco/opencode` — подтверждён (официальный, versioned tags напр. `:2.0.0`, docs.opencode.ai), пин в манифесте прогона; свой билд образа не нужен, зеркало в Gitea OCI — фолбэк при проблемах доступности; storage изолирован на воркер (`OPENCODE_DB`/volume, issue #30302).
- Gitea — трекер v0 (OpenProject auth сломан, TODO); токен админа — в OpenBao `secret/gitea/admin`.

## Подходы (рассмотрены)

- **A. Один инкремент-монолит** (executor + песочница + 3 MCP + E2E одним планом): все риски доходят до приёмки в конце; стена в opencode-in-pod проскальзывает на всё. Отклонён.
- **B. Срез M4a/M4b/M4c** (как M2a–d) — **выбран владельцем**: сначала de-risk самого неизвестного (OpenCode API + скорость LLM) локально, затем перенос той же сборки в k3s, затем MCP и полный E2E.
- **C. MCP-first** (серверы до executor): MCP без консьюмера — спекулятивный код. Отклонён.

## Решение B — три инкремента

### M4a — Executor + OpenCode-адаптер, локально (docker на хосте)

Ключевая непрерывность: **в M4a тот же пиновый образ крутится через docker на хосте** (colima даёт docker), в M4b та же сборка становится подом k3s. Меняется только рантайм-окружение, код адаптера — нет.

- **Контракт `Executor`** (в `agent_runtime`, control plane знает только интерфейс):
  ```python
  class Executor(Protocol):
      async def start(self, request: ExecutionRequest) -> ExecutionSession
  class ExecutionSession(Protocol):
      def events(self) -> AsyncIterator[ExecutionEvent]   # SSE-стрим opencode → наши события
      async def diff(self) -> str                          # GET /session/:id/diff
      async def status(self) -> ExecutionStatus
      async def stop(self) -> None
  ```
  `ExecutionRequest` собирается из вершины M3 (TaskVertex: done_when, paths_allow) + промпт-шаблон + координаты репо (url, base_branch, work_branch) + identity.
- **Тонкий клиент** к `opencode serve` — hand-written, минимальный (create session, send prompt, SSE events, diff), против `/openapi.json` пиновой версии; контракт-тест против живого serve фиксирует поверхность API. *(Моё решение: не генерировать SDK-клиент — генерат борется с mypy --strict; ADR-формулировку «генерируется по /openapi.json» трактуем как «соответствует /openapi.json».)*
- **Push — на воркере** (M4a/M4b): песочница не получает git write-креды вообще. Воркер забирает дифф (`GET /session/:id/diff`), применяет к host-side worktree клона, коммитит и пушит в Gitea своими кредами (токен из OpenBao). Источник истины по диффу — API opencode + host-side git. Аудит пуша — на воркере (audit-журнал M3). В M4c git MCP переносит push на серверную сторону с аудитом операций.
- **done_when vs схема M3**: колонка `done_when` в M3 — строка; M4 не меняет схему M3. Конвенция M4: строка содержит JSON `{"checks": [...]}`; не-парсится/пусто → **fail-closed** (вердикт FAIL с причиной `unparseable_done_when`) — код никогда не пропускает молча.
- **Воркер — run-scoped (v0)**: claim-ит вершины только известного `run_id` (из конфига/скрипта приёмки). Discovery-endpoint (claim-any) — кандидат в M5, когда рунов станет много; M4 код M3 не трогает.
- **Session log**: append-only NDJSON, строка = `{ts, seq, type, payload}`; типы: `run_started`, `prompt`, `harness_event` (прокси SSE), `done_when_check`, `result`. Переживает падение воркера (Pattern 19 brain/hands/session).
- **Манифест прогона** (JSON): vertex id, identity, образ+пин, модель, LLM-endpoint, бюджеты, timestamps, путь к session log, статистика диффа, вердикт done_when. С M4b — основа evidence pack.
- **done_when v0**: декларативные чеки на вершине, исполняются нашим кодом в песочнице после завершения агента: `{"checks": [{"type": "cmd", "run": "pytest -q"}, {"type": "files_exist", "paths": [...]}]}`. Реестр типов чеков, расширяемо. Дополнительно пост-проверка: изменённые файлы (из диффа) ⊆ paths_allow — иначе вершина fails.
- **Метрики токенов**: best-effort из SSE-событий opencode (если доступны в стриме); в манифест иначе `null` + TODO. Честно фиксируем, не обещаем.
- **Минимальный воркер-цикл** уже в M4a против живого agent_api (M3): claim → execute → heartbeat → complete/fail. Принимает: непрерывный poll, requeue при падении.
- **Крэш-резюм (v0)**: переигранная вершина стартует **с нуля** (fresh attempt), старый session log сохраняется как evidence, счётчик `attempts` из M3 растёт. Продолжение той же OpenCode-сессии — не v0 (plumbing session-id + семантика continuation), пересмотр после M4c.
- **Scratch-репо** `swarm-lab/target-scratch` в Gitea (не podinfo): сеется скриптом приёмки (микро-Python-проект + тесты); задача вершине — скриптованная («добавь функцию X, тесты проходят»).
- **Acceptance `m4a_flow.sh`**: сеет репо → создаёт run+вершину в agent_api → воркер claim-ит → OpenCode (docker, pin, LiteLLM) делает работу → session log + манифест записаны → done_when-вердикт PASS → дифф с веткой в Gitea. Плюс trio (m2a–d, m3) не сломаны.

### M4b — песочница в k3s + Vault-токен

- Pod-менеджер: initContainer клонирует репо из Gitea по ветке (read-креды — **только в initContainer**); PVC под repo+OPENCODE_DB; воркер на хосте создаёт/наблюдает поды через kubernetes API.
- **NetworkPolicy**: поду разрешены только Gitea, LiteLLM, MCP, DNS; остальной egress запрещён. **Проверяется спайком в начале M4b**; если k3d не энфорсит — любые пересоздания кластера только с явного апрува владельца (правило STATE.md), с вариантами на выбор.
- **Vault-токен**: короткоживущий, **воркер-брокеринг в v0** — воркер (своей identity) читает `secret/gitea/*` из OpenBao и инжектит в initContainer; k8s-auth OpenBao (per-project политики) — хардening при выходе за ноутбук. Основной контейнер — без git-кредов, push делает воркер (см. M4a).
- Session log и манифест — на PVC, после вершины выгружаются в evidence (пока — локальная директория, собрать в PR — в M4c).
- **Acceptance `m4b_flow.sh`**: тот же цикл, что m4a, но исполнитель — под в k3s с apply-политикой сети (негатив-тест: прямой доступ пода к Postgres запрещён).

### M4c — MCP-серверы + полный E2E

- **git MCP**: clone_status/create_branch/commit/push/create_pr — Gitea REST + git; токен из OpenBao; операции аудируются (action → audit M3). Работает server-side против worktree прогона (PVC в M4b); actor — identity из заголовка запроса.
- **task-source MCP**: get_task/list_tasks/set_status/add_comment поверх Gitea Issues (v0-трекер). Петля статусов: In Progress при claim, комментарии на PR-создание, Done/Blocked по вердикту — закрывает маcштаб петли из TODO.md.
- **llm MCP**: прокси к LiteLLM для вспомогательных вызовов агента вне основного harness-цикла.
- **Кред-модель MCP (v0)**: серверы держат admin-токен серверно, actor фиксируется в audit по identity; per-identity токены Gitea — hardening-бэклог (сейчас identities — реестр M3, не цреды).
- Всё — FastMCP, streamable HTTP, деплой в кластере, регистрация в конфиге OpenCode (`mcp.servers.*`).
- **Acceptance `m4c_flow.sh`**: полный цикл — Gitea issue → (существующий poller m2c) → run/вершина в agent_api → агент в поде (M4b-рантайм) → работа через MCP → PR с evidence (session log + манифест в описании/attachments) → статусы задачи In Progress → Done.

## Что остаётся билдеру (решать при реализации)

- Точный набор REST-эндпоинтов opencode для клиента (по живому serve в спайке).
- Разметка SSE-событий opencode → наши ExecutionEvent.
- Промпт-шаблон вершины (структура, языки, подача paths_allow/done_when агенту).
- Реестр типов чеков done_when — минимальный старт (cmd, files_exist), расширение по факту.

## Решения builder check (2026-11-09, ревьюер-субагент)

Вопросы ревьюера, закрытые из контекста проекта (отдельного раунда владельцу не потребовалось):

1. **Push до git MCP** → воркер пушит сам (host-side worktree), песочница без write-кредов; git MCP (M4c) переносит push на серверную сторону.
2. **done_when против строки M3** → схему M3 не меняем; конвенция JSON в строке; не-парсится → fail-closed.
3. **Hand-written клиент** → да, hand-written по `/openapi.json` пина + контрак-тест (ADR-формулировка трактуется как «соответствует»).
4. **Discovery воркера** → v0 run-scoped (run_id из конфига); claim-any endpoint — кандидат M5, код M3 в M4 не трогаем.
5. **Vault в M4b** → воркер-брокеринг: воркер читает секрет и инжектит в initContainer; k8s-auth — хардening позже.
6. **Крэш-резюм** → fresh attempt с сохранением evidence, attempts++ (M3); продолжение сессии — не v0.
7. **Происхождение образа** → официальный `ghcr.io/anomalyco/opencode` существует (docs.opencode.ai), пин апстрим-тега; свой билд не нужен, зеркало в Gitea OCI — фолбэк.
8. **Креды MCP (M4c)** → admin-токен серверно + audit по identity; per-identity токены — hardening-бэклог.
9. **NetworkPolicy на k3d** → проверка спайком в начале M4b; пересоздание кластера — только с явного апрува владельца (STATE.md).

## Риски

- **Скорость локального LLM**: qwen2.5-coder:7b на 6CPU — минуты на шаг; приёмка с ретраями и щедрыми таймаутами.
- **OpenCode API нестабилен** между версиями — лечится пином и контрак-тестом; вторым адаптером — Codex (ADR-0002).
- **`ask`-разрешения зависают в headless** — v0 без ask; HITL-мост отдельно после M5.
- **SSE через docker/k3s** — проверить стрим в спайке до затвердевания контракта событий.

## Спайк (бросовый, внутри M4a, задача 1)

Поднять `opencode serve` в docker (пин) с LLM через LiteLLM: один промпт по REST, стрим SSE, `GET diff`. Подтвердить поверхность API и скорость. Бросовый: без тестов; перенесённое в прод-код — через TDD.
