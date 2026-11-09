# M4a: Executor contract + OpenCode adapter (local docker) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Первый исполнитель вершин: контракт `Executor` + адаптер OpenCode (`opencode serve` в docker, pin образа), session log NDJSON, манифест прогона, done_when-валидация кодом (fail-closed), paths_allow-проверка и run-scoped воркер против живого agent_api.

**Architecture:** `packages/agent_runtime` — httpx-клиент к REST/SSE `opencode serve` (пиновый образ `ghcr.io/anomalyco/opencode`), SSE-стрим перекачивается в append-only NDJSON session log (Pattern 19). Воркер claim-ит вершины из M3 `agent_api` (run-scoped), ждёт завершения сессии, валидирует done_when и paths_allow кодом и завершает вершину `complete`/`fail`. В M4b docker-спавн заменяется k3s-подом без изменения кода адаптера.

**Tech Stack:** Python ≥3.12, pydantic v2, pydantic-settings, httpx (SSE — свой парсер строк, без новых зависимостей), pytest + anyio, subprocess git/docker.

**Spec:** `docs/superpowers/specs/2026-11-09-m4-agent-runtime-design.md` (M4a-раздел), спека §10.3 (M4), ADR-0002.

## Global Constraints

- После каждой задачи зелёные: `uv run ruff check .` · `uv run mypy packages apps` · `uv run pytest` (trio-дисциплина).
- `packages/agent_runtime` НЕ импортирует FastAPI/SQLAlchemy — контроль plane только по HTTP (httpx). Граница пакета.
- Код/комментарии — English only (AGENTS.md §7). Коммиты — явные пути, никакого `git add -A` (AGENTS.md §9).
- mypy --strict, ноль подавлений; тесты герметичны: docker и LLM — только в задаче 10 (acceptance) и спайке (задача 1).
- Образ OpenCode — пин из задачи 1, константа `OPENCODE_IMAGE` в `agent_runtime/settings.py`; хардкод версии в других местах запрещён.
- done_when: строка-JSON `{"checks": [...]}`; не парсится/пусто → **fail-closed** (`unparseable_done_when`); неизвестный тип чека → fail-closed.
- paths_allow: пустой список = любые изменения запрещены (fail-closed); проверка — `fnmatch` по списку.
- Dev-зависимости `agent_runtime` — как в `apps/agent_api`: `["httpx2>=2.16"]`; рантайм: `["httpx>=0.28", "pydantic>=2.9", "pydantic-settings>=2.6"]`.

## Review Focus

1. **Пустой/битый done_when молча проходит** — недопустимо: вердикт обязан быть FAIL с причиной `unparseable_done_when`. Тест: Task 3.
2. **Неизвестный тип чека treated as pass** — недопустимо: fail-closed. Тест: Task 3.
3. **Дифф трогает путь вне paths_allow** (в т.ч. пустой allow-лист) → вершина FAIL до запуска done_when. Тесты: Task 3 (pathguard), Task 9 (интеграция в воркере).
4. **Lease истёк (heartbeat 409)** — воркер обязан прекратить работу вершины, никогда не звать `complete`; вершина уходит в requeue через `fail`. Тест: Task 9.
5. **SSE-стрим оборвался посреди прогона** — сессия не должна репортить DONE (статус RUNNING/FAILED), false-positive завершения исключён. Тест: Task 7.

---

### Task 1: Spike — opencode serve в docker через LiteLLM (throwaway)

**Files:**
- Create: `docs/superpowers/research/2026-11-09-m4a-opencode-serve-spike.md` (заметка по итогам)

**Interfaces:**
- Produces: пин версии (точный тег образа), рабочие флаги `opencode serve` (порт, hostname), способ конфига провайдера (LiteLLM OpenAI-compatible endpoint + model alias), форма SSE-событий и ответа `GET .../diff` — всё фиксируется в заметке и используется в Tasks 6–7 и константе `OPENCODE_IMAGE`.

- [ ] **Step 1: Запустить в docker** `ghcr.io/anomalyco/opencode:<актуальный versioned tag>` с `serve`, LLM — LiteLLM `:30084` (qwen2.5-coder:7b). Подобрать флаги/config провайдера, чтобы сессия отвечала.
- [ ] **Step 2: Прогнать по REST** один промпт: создать сессию, отправить сообщение, прочитать SSE-стрим, забрать diff — зафиксировать точные пути эндпоинтов и форму событий (JSON-примеры — в заметку).
- [ ] **Step 3: Записать заметку** (версия образа, флаги, эндпоинты, примеры SSE/diff, время ответа модели) — это контракт для Tasks 6–7. Спайк в git не кодится.

### Task 2: Контракт Executor + Fake

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/executor.py`
- Test: `packages/agent_runtime/tests/test_executor_contract.py`

**Interfaces:**
- Consumes: ничего (фундамент).
- Produces: `ExecutionStatus(StrEnum)` = `running|done|failed|stopped`; `ExecutionRequest(BaseModel)` поля `run_id: int, vertex_id: int, name: str, prompt: str, paths_allow: list[str], done_when: str, repo_url: str, base_branch: str = "main", work_branch: str`; `ExecutionEvent(BaseModel)` поля `ts: datetime, type: str, payload: dict[str, Any]`; `ExecutionResult(BaseModel)` поля `diff: str, diff_files: list[str], session_log_path: Path, manifest_path: Path, tokens_used: int | None = None`; `ExecutionSession(Protocol)` методы `events() -> AsyncIterator[ExecutionEvent]`, `wait_done() -> ExecutionStatus`, `diff() -> str`, `status() -> ExecutionStatus`, `stop() -> None`, property `result() -> ExecutionResult | None`; `Executor(Protocol)` метод `start(request: ExecutionRequest) -> ExecutionSession`.

- [ ] **Step 1: Write the failing test** — `test_fake_executor_satisfies_contract`: `FakeExecutor` (в тест-файле) возвращает сессию с фиксированными `events/diff/status/result`; проверить `isinstance(fake, Executor)` (протоколы `@runtime_checkable`), `wait_done() == ExecutionStatus.DONE`, round-trip `ExecutionRequest.model_validate(request.model_dump())`.
- [ ] **Step 2: Run to verify it fails** — `uv run pytest packages/agent_runtime/tests/test_executor_contract.py -v` → FAIL (`executor` не существует).
- [ ] **Step 3: Implement** — `executor.py`: только перечисленные типы, протоколы `@runtime_checkable`. Тел (кроме моделей) нет.
- [ ] **Step 4: Run to verify it passes** — та же команда → PASS.
- [ ] **Step 5: Commit** — `git add packages/agent_runtime/src/agent_runtime/executor.py packages/agent_runtime/tests/test_executor_contract.py && git commit -m "feat(agent_runtime): Executor contract and models"`

### Task 3: Session log (NDJSON, append-only)

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/session_log.py`
- Test: `packages/agent_runtime/tests/test_session_log.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `class SessionLog` — `__init__(path: Path)`; `append(type_: str, payload: dict[str, Any]) -> None` (строка `{ts, seq, type, payload}`, `ts` — UTC ISO-8601, `seq` с 1, продолжается после переоткрытия файла); `read_all() -> list[dict[str, Any]]`; `iter_type(type_: str) -> Iterator[dict[str, Any]]`.

- [ ] **Step 1: Write the failing test** — `test_append_read_roundtrip` (append 2 записи → `read_all()` равен, поля ts/seq/type/payload на месте); `test_seq_continues_after_reopen` (новый `SessionLog` на тот же файл продолжает seq с 3); `test_iter_type_filters`.
- [ ] **Step 2: Run to verify it fails** — FAIL (нет модуля).
- [ ] **Step 3: Implement** — запись в режиме `"a"`, `json.dumps(..., ensure_ascii=False)`; при `__init__` прочитать последний seq, если файл есть.
- [ ] **Step 4: Run to verify it passes** → PASS; затем `uv run pytest` (все) → PASS.
- [ ] **Step 5: Commit** — `... -m "feat(agent_runtime): append-only NDJSON session log"`

### Task 4: done_when-валидатор + pathguard (fail-closed)

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/done_when.py`, `packages/agent_runtime/src/agent_runtime/pathguard.py`
- Test: `packages/agent_runtime/tests/test_done_when.py`, `packages/agent_runtime/tests/test_pathguard.py`

**Interfaces:**
- Consumes: ничего.
- Produces (done_when.py): `DoneWhenError(ValueError)`, `DoneWhenUnparseable(DoneWhenError)`; `DoneWhenCheck(BaseModel)` поля `type: str, run: str | None = None, paths: list[str] | None = None`; `DoneWhenSpec(BaseModel)` поле `checks: list[DoneWhenCheck]`; `CheckResult(BaseModel)` поля `check: DoneWhenCheck, passed: bool, detail: str`; `DoneWhenVerdict(BaseModel)` поля `passed: bool, reason: str | None = None, results: list[CheckResult]`; `parse_done_when(raw: str) -> DoneWhenSpec` (пустая/битая → `DoneWhenUnparseable`); `validate_done_when(spec: DoneWhenSpec, *, cwd: Path, run_cmd: Callable[[str, Path], int] | None = None) -> DoneWhenVerdict` — `cmd` → `run_cmd(cmd, cwd) == 0` (дефолт: subprocess shell), `files_exist` → все пути относительно `cwd` существуют; неизвестный тип → `passed=False, reason="unknown check type: X"`.
- Produces (pathguard.py): `diff_files(diff: str) -> list[str]` (пути из строк `+++ b/<path>`, `/dev/null` пропускается); `filter_violations(changed: list[str], paths_allow: list[str]) -> list[str]` (`fnmatch.fnmatch`; пустой `paths_allow` → все `changed` нарушают).

- [ ] **Step 1: Write the failing tests** — done_when: `test_parse_valid`, `test_parse_empty_is_unparseable` (и `"   "`, и `"not json"`, и `"[]"` без ключа `checks` → `DoneWhenUnparseable`), `test_cmd_pass_and_fail` (`run_cmd`-заглушка), `test_files_exist_pass_and_fail`, `test_unknown_check_type_fails_closed` (вердикт `passed=False`); pathguard: `test_diff_files_parses_paths`, `test_allow_violation_detected`, `test_empty_allow_list_violates_everything`, `test_glob_allows_subdir`.
- [ ] **Step 2: Run to verify they fail** — FAIL (нет модулей).
- [ ] **Step 3: Implement** — сигнатуры выше; парсер — `json.loads` + `model_validate`; проверка порядка чеков — по порядку спецификации, первый FAIL останавливает (`results` содержит выполненные).
- [ ] **Step 4: Run to verify they pass** → PASS.
- [ ] **Step 5: Commit** — `... -m "feat(agent_runtime): code-validated done_when and paths_allow guard"`

### Task 5: Манифест прогона

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/manifest.py`
- Test: `packages/agent_runtime/tests/test_manifest.py`

**Interfaces:**
- Consumes: `ExecutionRequest` (Task 2).
- Produces: `build_manifest(*, request: ExecutionRequest, image: str, model: str, llm_endpoint: str, ts_start: datetime, ts_end: datetime | None, session_log_path: Path, diff_stat: str | None = None, tokens_used: int | None = None) -> dict[str, Any]`; `write_manifest(path: Path, manifest: dict[str, Any]) -> None` (JSON, indent=2, перезапись).

- [ ] **Step 1: Write the failing test** — `test_manifest_has_required_keys` (run_id, vertex_id, name, image, model, llm_endpoint, ts_start/ts_end, session_log_path, diff_stat, tokens_used); `test_write_and_reread` (JSON на диске равен dict).
- [ ] **Step 2: Run to verify it fails** → FAIL.
- [ ] **Step 3: Implement** — чистые функции, без побочных эффектов кроме `write_manifest`.
- [ ] **Step 4: Run to verify it passes** → PASS.
- [ ] **Step 5: Commit** — `... -m "feat(agent_runtime): run manifest"`

### Task 6: Тонкий клиент opencode serve

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/opencode_client.py`
- Test: `packages/agent_runtime/tests/test_opencode_client.py`

**Interfaces:**
- Consumes: эндпоинты и формы из заметки Task 1 (в плане помечены `⟨spike⟩` — заменить на подтверждённые пути).
- Produces: `OpencodeError(RuntimeError)`; `class OpencodeClient` — `__init__(http: httpx.AsyncClient)`; `create_session() -> str`; `send_prompt(session_id: str, text: str) -> None`; `events(session_id: str) -> AsyncIterator[dict[str, Any]]` (SSE-стрим: строки `event:`/`data:`, пустая строка — конец события; `data` — JSON); `diff(session_id: str) -> str`. HTTP ≠ 2xx → `OpencodeError` с телом ответа.

- [ ] **Step 1: Write the failing tests** (httpx `MockTransport`) — `test_create_session_returns_id`, `test_send_prompt_posts_message`, `test_events_parses_sse_stream` (скриптованный стрим из двух событий + обрыв — итератор отдаёт только валидные), `test_diff_returns_text`, `test_http_error_raises_opencode_error`.
- [ ] **Step 2: Run to verify they fail** → FAIL.
- [ ] **Step 3: Implement** — `⟨spike⟩` эндпоинты: создание сессии, отправка сообщения, SSE-подписка, дифф; SSE-парсер — свой, по строкам (`httpx.AsyncClient.stream`).
- [ ] **Step 4: Run to verify they pass** → PASS.
- [ ] **Step 5: Commit** — `... -m "feat(agent_runtime): thin opencode serve client"`

### Task 7: Адаптер OpencodeExecutor + docker-спавн

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/opencode_executor.py`, `packages/agent_runtime/src/agent_runtime/settings.py`
- Modify: `packages/agent_runtime/src/agent_runtime/settings.py` — создаётся здесь (константа пина), расширяется в Task 9
- Test: `packages/agent_runtime/tests/test_opencode_executor.py`

**Interfaces:**
- Consumes: `Executor/ExecutionSession` (Task 2), `SessionLog` (Task 3), `OpencodeClient` (Task 6), `build_manifest/write_manifest` (Task 5).
- Produces: `agent_runtime/settings.py` — `OPENCODE_IMAGE: str = "ghcr.io/anomalyco/opencode:<tag из заметки Task 1>"` (единственный источник пина). `class DockerServe` — `__init__(*, image: str, repo_dir: Path, port: int, env: dict[str, str], runner: Callable[[list[str]], None] | None = None)`; `start() -> str` (base_url; runner-инъекция для тестов — проверяется argv: `-v repo:/workspace`, `-p port:⟨spike⟩`, образ из пина); `stop() -> None`. `class OpencodeExecutor` — реализует `Executor`; `__init__(*, base_url: str | None, workspace: Path, image: str = OPENCODE_IMAGE, model: str, llm_endpoint: str, port: int = 4096)` (`base_url` задан → подключение без спавна); `build_prompt(request: ExecutionRequest) -> str` (имя вершины, промпт, paths_allow, done_when — человекочитаемо). Сессия: SSE качается в фоновую задачу → `SessionLog.append("harness_event", ...)` + очередь для `events()`; `tokens_used` — best-effort из событий (`null` если нет); завершение по терминальному событию `⟨spike⟩`; обрыв стрима без терминального события → `status == failed` (Review Focus 5), `wait_done()` не возвращает `done`.

- [ ] **Step 1: Write the failing tests** — `test_docker_serve_builds_expected_argv` (runner-инъекция), `test_start_returns_session_and_streams_events` (MockTransport: сессия → 2 события → терминал; `events()` отдаёт оба, session log содержит `prompt` + `harness_event`), `test_diff_and_result_after_done`, `test_stream_cut_is_not_done` (стрим оборван → `wait_done() == failed`, `result is None`), `test_stop_stops`.
- [ ] **Step 2: Run to verify they fail** → FAIL.
- [ ] **Step 3: Implement** — сигнатуры выше; манифест пишется на `start()` (ts_end=None) и финализируется на `wait_done()`.
- [ ] **Step 4: Run to verify they pass** → PASS; `uv run mypy packages` → PASS.
- [ ] **Step 5: Commit** — `... -m "feat(agent_runtime): OpencodeExecutor adapter with docker serve launcher"`

### Task 8: gitops — clone/push диффа на воркере

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/gitops.py`
- Test: `packages/agent_runtime/tests/test_gitops.py`

**Interfaces:**
- Consumes: ничего (subprocess git).
- Produces: `ensure_repo(workspace: Path, repo_url: str, base_branch: str, *, token: str | None = None) -> Path` (локальный «remote» поддерживается для тестов; clone если нет, иначе `fetch` + `reset --hard origin/<base>`); `push_diff(repo_dir: Path, diff: str, work_branch: str, *, message: str) -> str` (apply диффа → commit → push ветки; возвращает sha).

- [ ] **Step 1: Write the failing tests** — `test_ensure_repo_clones_and_resets` (tmp bare-remote, второй вызов сбрасывает на base), `test_push_diff_creates_branch_and_commit` (sha валиден, файл в ветке содержит правку, дифф применён на base).
- [ ] **Step 2: Run to verify they fail** → FAIL.
- [ ] **Step 3: Implement** — subprocess `git`, `check=True`; креды через env `GIT_AUTHOR_*`/пустой committer (имя `agent-runtime`).
- [ ] **Step 4: Run to verify they pass** → PASS.
- [ ] **Step 5: Commit** — `... -m "feat(agent_runtime): worker-side git clone and diff push"`

### Task 9: Воркер (run-scoped) против agent_api

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/worker.py`
- Modify: `packages/agent_runtime/src/agent_runtime/settings.py` (расширить: `WorkerSettings` рядом с константой пина)
- Test: `packages/agent_runtime/tests/test_worker.py`

**Interfaces:**
- Consumes: API M3 — `POST /vertices/claim` `{run_id, agent, ttl_seconds}` → `{"vertex": {...}|null}`; `POST /vertices/{id}/heartbeat` `{agent, ttl_seconds}` (409 если lease чужой/истёк); `POST /vertices/{id}/complete` `{agent, result}`; `POST /vertices/{id}/fail` `{agent}` — точные пути `apps/agent_api/src/agent_api/main.py:72-111`. Модули: Task 2, 4, 6 (опц.), 7, 8.
- Produces: `WorkerSettings(BaseSettings)` env_prefix `AGENT_RUNTIME_`: `api_url: str, run_id: int, agent: str = "worker", poll_interval_seconds: float = 2.0, lease_ttl_seconds: int = 300, workspace_dir: Path = Path("./.agent-runtime-workspace"), repo_url: str, base_branch: str = "main", opencode_base_url: str | None = None, opencode_port: int = 4096, litellm_base_url: str, litellm_api_key: str, model: str, git_token: str = ""`; `ControlPlaneClient(http: httpx.AsyncClient, settings)` методы `claim() -> dict | None`, `heartbeat(vertex_id: int) -> bool`, `complete(vertex_id: int, result: dict) -> None`, `fail(vertex_id: int) -> None`; `async def worker_loop(settings: WorkerSettings, executor: Executor, *, http: httpx.AsyncClient | None = None, stop: asyncio.Event | None = None) -> None`; `def main() -> None` (точка входа `python -m agent_runtime.worker`).

- [ ] **Step 1: Write the failing tests** (control plane — httpx `MockTransport` со stateful-заглушкой; executor — `FakeExecutor` из Task 2 с настраиваемым diff/result) — `test_happy_path_claims_executes_completes` (claim → вершина; complete вызван с `result` содержащим `branch`, `diff_files`, `verdict.passed=True`, `session_log_path`); `test_done_when_unparseable_fails_vertex` (вершина с `done_when="junk"` → `fail`, reason в audit-payload `fail`); `test_paths_violation_fails_vertex` (FakeExecutor меняет файл вне allow-листа → `fail`); `test_heartbeat_conflict_aborts_without_complete` (heartbeat 409 → ни `complete`, ни повторного `complete`; вершина брошена); `test_executor_exception_fails_vertex` (FakeExecutor кидает → `fail`); `test_empty_claim_sleeps` (claim → null, цикл жив).
- [ ] **Step 2: Run to verify they fail** → FAIL.
- [ ] **Step 3: Implement** — `worker_loop`: цикл claim → на вершину: `ensure_repo` → `OpencodeExecutor.start(request)` → задача heartbeat (каждые `lease_ttl/3`; 409 → `stop()` сессии, без complete) → `wait_done()` → `diff()` → `diff_files`/`filter_violations` → нарушения → `fail`; иначе `parse_done_when`/`validate_done_when(cwd=repo_dir)` → `passed` → `complete(result)` : `fail(reason)`; `push_diff` веткой `agent/vertex-{id}` перед complete, `result["branch"]`. Любое исключение на вершине → `fail` (requeue M3), цикл продолжает. `stop` → выход.
- [ ] **Step 4: Run to verify they pass** → PASS; `uv run pytest` весь воркспейс → PASS.
- [ ] **Step 5: Commit** — `... -m "feat(agent_runtime): run-scoped vertex worker over agent_api"`

### Task 10: Acceptance m4a_flow.sh на живом стенде

**Files:**
- Create: `infra/tests/m4a_flow.sh`
- Modify: `ARCH.md` (§8 verification map: строка m4a_flow), `STATE.md` (по завершении инкремента)

**Interfaces:**
- Consumes: живой стенд M2/M3 (Gitea :30080, LiteLLM :30084, Postgres NodePort 15432, agent_api как в m3_flow.sh), `python -m agent_runtime.worker` (Task 9), пин Task 1.

- [ ] **Step 1: Скрипт** (bash, `set -euo pipefail`, стиль `m3_flow.sh`): (1) admin-токен Gitea из OpenBao; (2) создать/пересеять репо `swarm-lab/target-scratch` (микро-проект: `pyproject.toml`, `src/wordtools/__init__.py`, `tests/test_wordtools.py` с одним зелёным тестом) ветка `main`; (3) `POST /runs` + `POST /runs/{id}/vertices` — вершина `add-reverse-words`: `paths_allow=["src/wordtools/**","tests/**","pyproject.toml"]`, `done_when='{"checks":[{"type":"cmd","run":"pytest -q"},{"type":"files_exist","paths":["src/wordtools/reverse.py"]}]}'`, промпт в `result`-независимом поле вершины `name`/`done_when` (текст задачи — в промпт-шаблоне воркера: добавить `reverse_words` в `src/wordtools/reverse.py` + тест); (4) запустить воркер (`AGENT_RUNTIME_*` env, `OPENCODE_BASE_URL` пуст → docker-спавн) в фоне; (5) поллинг psql через NodePort 15432 (db `swarm`) — `vertices.status='done'`, таймаут 20 мин (медленный LLM — норма); (6) проверки: ветка `agent/vertex-*` в Gitea содержит правку; `session_log.ndjson` существует и содержит `harness_event`; `manifest.json` содержит пин образа, модель и `verdict.passed=true`.
- [ ] **Step 2: Прогнать на живом стенде** — `./infra/tests/m4a_flow.sh` → PASS (приложить вывод).
- [ ] **Step 3: Регрессии** — `./infra/tests/m2a_pr_flow.sh && ./infra/tests/m2b_deploy_flow.sh && ./infra/tests/m2c_flow.sh && ./infra/tests/m2d_flow.sh && ./infra/tests/m3_flow.sh` → PASS.
- [ ] **Step 4: Код-гейты** — `uv run ruff check . && uv run mypy packages apps && uv run pytest` → PASS.
- [ ] **Step 5: Commit** — `git add infra/tests/m4a_flow.sh ARCH.md && git commit -m "test(m4a): acceptance flow for local executor vertex"`

## Self-review notes

- Спека M4a покрыта: контракт (T2), session log (T3), done_when+paths_allow (T4), манифест (T5), клиент (T6), адаптер+пин (T7, T1), worktree/push воркера (T8), claim-loop (T9), acceptance (T10). Пустые заготовки `tests/test_smoke.py` и `pyproject.toml` пакета — обновить в T2 (deps) без отдельной задачи.
- K3s/Vault/NetworkPolicy/MCP — сознательно не здесь (M4b/M4c).
