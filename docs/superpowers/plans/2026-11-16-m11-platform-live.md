# M11 Platform Live Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (owner pre-authorized inline execution).

**Goal:** the platform runs as a standing service on the stand.
- A `swarm`-labelled issue in an onboarded repo is picked up, planned, executed and judged with no flow script.
- The panel is always on at `localhost:30400/ui`.
- Automations fire on cron.

**Architecture:**
- The control plane (agent_api + panel) is an Argo CD app in namespace `platform`.
- A host daemon container (toolbox image, pinned worktree `.platform-live/`) runs the dispatcher and the automation scheduler/poller.
- The dispatcher spawns run-scoped worker subprocesses (the M8 `swarm run` shape).

**Tech Stack:** Python 3.12, FastAPI, httpx, pydantic v2, croniter, kustomize/Argo CD, Playwright (docker).

**Spec:** `docs/superpowers/specs/2026-11-16-m11-platform-live-design.md`

## Global Constraints

- **Panel/API:** NodePort **30400**, reachable on loopback only. Owner user `owner`, password at OpenBao `secret/platform/ui`, printed only by `platform.sh password`.
- **Intake:**
  - org `swarm-lab`; issue label `swarm`;
  - per-repo `.platform/project.yaml` = `{intake: bool, paths: [glob], done_when: str, deploy?: {app, namespace}, workers_per_run?: int (1..3, default 3)}`;
  - `task_ref` = `owner/repo#N`, run `project` = `owner/repo`.
- **Caps:**
  - `max_active_runs` = 2;
  - at most 11 sandboxes in total;
  - `workers_per_run` ≤ 3;
  - dispatcher tick 30 s.
- **Secrets** are read from OpenBao by the daemon at startup (vault_broker pattern). They are never in `docker run -e`, never logged.
- **Images:** pinned tags only. Playwright `mcr.microsoft.com/playwright/python:v1.49.0-noble`; `croniter==6.0.0`.

## Review Focus

1. **Dispatcher restarted between "run created" and "plan posted":** the run has 0 vertices. The next tick re-plans it; it does not create a second run. Test: `test_tick_replans_run_without_vertices`.
2. **Invalid or missing `.platform/project.yaml`:** the repo is skipped with a logged reason, never crashing the tick. Test: `test_tick_skips_repo_with_invalid_config`.
3. **Gitea or agent_api unreachable for one tick:** the tick logs the error and the loop survives. Test: `test_daemon_loop_survives_tick_error`.
4. **Cron on first start (no previous run):** the scheduler does not fire every past tick, at most once. Test: `test_due_first_start_fires_once`.
5. **Issue closed while its run is active:** the dispatcher does not spawn more workers for it, and still judges what exists. Test: `test_tick_does_not_respawn_for_closed_issue`.

---

### Task 1: Run lookup in the control plane

**Files:** Modify `apps/agent_api/src/agent_api/store.py` and `main.py`. Test `apps/agent_api/tests/test_runs_list.py`.

**Produces:** `GET /runs?task_ref=&project=&status=&limit=50` → `{"runs": [{"id", "project", "task_ref", "status", "vertex_count"}]}`, newest first.

- [ ] Write the tests, then FAIL:
  - `test_runs_filter_by_task_ref` (two runs; the filter returns one);
  - `test_runs_filter_by_status`;
  - `test_runs_vertex_count`.
- [ ] Implement `store.list_runs(session, *, task_ref="", project="", status="", limit=50) -> list[dict]` plus the route → PASS. Commit.

### Task 2: Per-project config

**Files:**
- Create `packages/agent_runtime/src/agent_runtime/project_config.py`.
- Modify `apps/bootstrap/src/bootstrap/cli.py` (onboard writes it; new `project-config put --repo o/r [--intake]`) and `apps/bootstrap/src/bootstrap/profiles.py` (default `done_when`/`paths` per profile).
- Tests: `packages/agent_runtime/tests/test_project_config.py`, `apps/bootstrap/tests/test_project_config_cli.py`.

**Produces:**
- `ProjectConfig(intake: bool, paths: list[str], done_when: str, deploy: DeployTarget | None, workers_per_run: int)`;
- `parse_project_config(text: str) -> ProjectConfig` (raises `ValueError`);
- `profile_project_config(profile: str) -> dict`, where:
  - python → `done_when` `"uv sync --quiet --all-groups && uv run --quiet pytest -q"`, `paths` `["src/**","tests/**"]`;
  - typescript → the m9 docker-node command, `paths` `["src/**","test/**"]`.

- [ ] Write the tests, then FAIL:
  - valid parse;
  - an empty `done_when` → `ValueError`;
  - `workers_per_run` 0 or 4 → `ValueError`;
  - an unknown key → `ValueError`;
  - onboard puts `.platform/project.yaml` with `intake: true`.
- [ ] Implement → PASS. Commit.

### Task 3: Dispatcher

**Files:** Create `packages/agent_runtime/src/agent_runtime/dispatcher.py`. Test `packages/agent_runtime/tests/test_dispatcher.py`.

**Interfaces:**
- Consumes: `GET /runs?task_ref=` (Task 1), `parse_project_config` (Task 2), `planner.plan_task`, `swarm.post_plan`, `swarm.judge_run`, `swarm.worker_env`.
- Produces:
  - `Task(repo: str, number: int, title: str, body: str, open: bool)`;
  - `Ports` protocol:
    - `list_repos() -> list[str]`, `read_config(repo) -> str | None`;
    - `list_tasks(repo) -> list[Task]` (open issues labelled `swarm`), `issue_open(repo, n) -> bool`;
    - `comment(repo, n, text)`;
    - `plan(run_id, task, cfg) -> int` (number of vertices);
    - `spawn(run_id, repo, cfg, workers) -> list[Proc]`;
    - `judge(run_id, repo, cfg) -> JudgeVerdict`;
    - `api_get(path, params) -> dict`, `api_post(path, json) -> dict`;
  - `class Dispatcher(ports, *, max_active_runs=2, sandbox_cap=11)` with `async tick() -> list[str]` (event lines) and `async adopt() -> None`.

**Tick algorithm (decided here):**
1. Reap finished worker procs.
2. For each active run: if all vertices are terminal and the run is `pending`, judge it and comment the verdict.
3. Else, if no live procs and the issue is still open, respawn its workers.
4. Count active runs.
5. For each repo with `intake`, for each task without a run, create a run while active < `max_active_runs` and the sandbox budget allows `cfg.workers_per_run`.
6. Plan a run with 0 vertices.
7. Spawn `min(vertices, workers_per_run)` workers.
8. Comment the plan.

- [ ] Write the tests with fake ports, then FAIL:
  - `test_tick_creates_run_plans_and_spawns`;
  - `test_tick_skips_task_with_existing_run`;
  - `test_tick_respects_max_active_runs`;
  - `test_tick_replans_run_without_vertices`;
  - `test_tick_judges_terminal_run_and_comments`;
  - `test_tick_skips_repo_with_invalid_config`;
  - `test_tick_skips_repo_with_intake_false`;
  - `test_adopt_respawns_workers_for_pending_runs`;
  - `test_tick_does_not_respawn_for_closed_issue`.
- [ ] Implement → PASS. Commit.

### Task 4: Cron scheduler for automations

**Files:** Modify `packages/agent_runtime/src/agent_runtime/automations.py`, `pyproject` (`croniter==6.0.0` and its types/stub or a typed wrapper) and `infra/automations/vuln-scan.yaml` (`trigger: cron`, `schedule: "0 6 * * 1"`). Test `packages/agent_runtime/tests/test_automation_schedule.py`.

**Produces:** `due(definition: dict, last_run_at: datetime | None, now: datetime, started_at: datetime) -> bool`, true iff `trigger == "cron"` and the latest tick ≤ `now` is after `last_run_at` (or after `started_at` when there was never a run). Also `async schedule_tick(api, definitions, now, started_at) -> list[str]`, which POSTs `{"name", "requested_by": "cron"}`.

agent_api must expose `created_at` on automation runs; check `AutomationRunRow` and add the field if missing (with a test).

- [ ] Write the tests, then FAIL:
  - `test_due_manual_never`;
  - `test_due_after_tick`;
  - `test_not_due_before_tick`;
  - `test_due_first_start_fires_once`;
  - `test_schedule_tick_posts_requested_run`.
- [ ] Implement → PASS. Commit.

### Task 5: The daemon

**Files:** Create `packages/agent_runtime/src/agent_runtime/daemon.py` and `infra/scripts/platform.sh`. Test `packages/agent_runtime/tests/test_daemon.py`.

**Produces:**
- `build_worker_base_env(settings: DaemonSettings, secrets: Secrets) -> dict[str, str]`, the selfdev_flow worker env: k3s backend, in-cluster LiteLLM, MCP pod URLs, task-source host URL, `GATES_ENABLED=1`, reviewer `http://localhost:30084/v1`, prometheus 30090, deploy per `cfg.deploy`.
- `async run_loops(dispatcher, automations, *, interval_s) -> None`, which catches and logs per-tick exceptions.

**Secrets:** read via `vault_broker`: the Gitea agent token and the LiteLLM master key at `secret/litellm/master`. The worker resolves its own git token from OpenBao, so `AGENT_RUNTIME_GIT_TOKEN` is not set.

**`platform.sh` commands:**

| Command | What it does |
|---|---|
| `up` | 1. Unseal OpenBao, then apply the agents section and import the opencode-git image. 2. Update the `.platform-live` worktree to `origin/main` (or `--ref`). 3. Build `swarm-platform:<sha7>`. 4. Run `bootstrap platform` (Task 6), then set the Argo image override. 5. Wait for Healthy. 6. `docker run -d --restart unless-stopped --name swarm-platform-daemon` (toolbox image, host network, docker socket, kubeconfig, repo path mount) running `uv run python -m agent_runtime.daemon` in `.platform-live`. |
| `down` | Stops the daemon container. |
| `restart` | Restarts it. |
| `status` | Shows the daemon state. |
| `logs` | Shows the daemon logs. |
| `password` | Prints the panel password. |

- [ ] Write the tests, then FAIL:
  - `test_worker_env_has_no_git_token`;
  - `test_worker_env_deploy_from_config`;
  - `test_daemon_loop_survives_tick_error`;
  - `test_secrets_not_in_repr`.
- [ ] Implement → PASS. `bash -n infra/scripts/platform.sh`. Commit.

### Task 6: Control plane in-cluster

**Files:**
- Create `infra/platform/Dockerfile` (`python:3.12-slim` pinned and `uv`; copies the workspace; `uv sync --frozen --no-dev --package agent-api`; `CMD uvicorn --factory agent_api.main:create_app --host 0.0.0.0 --port 8000`; `AGENT_API_AUTOMATIONS_DIR=/app/infra/automations`).
- Create `infra/platform/control-plane/{kustomization,deployment,service,servicemonitor}.yaml` and `infra/argocd/application-platform-control.yaml`.
- Add a bootstrap section `platform` (`infra/scripts/bootstrap.sh`): the `owner` password generated once into `secret/platform/ui`, the session secret, and the DB URL in Secret `agent-api`.
- Test `apps/bootstrap/tests/test_platform_manifests.py`.

**Values:** Deployment `agent-api`, ns `platform`, `imagePullPolicy: Never`, readiness `/healthz`, env from Secret `agent-api`; Service NodePort 30400 → 8000; ServiceMonitor label as in `infra/monitoring/values.yaml` selectors.

- [ ] Write the tests, then FAIL:
  - NodePort 30400;
  - `imagePullPolicy` Never;
  - every env value from `secretKeyRef` (no literal secrets);
  - the Argo app path `infra/platform/control-plane` and repo Gitea `swarm-lab/ai-based-isv`;
  - the Dockerfile base pinned by tag.
- [ ] Implement → PASS. Commit.

### Task 7: Browser e2e of the panel

**Files:** Create `infra/tests/e2e/test_panel.py` and `infra/tests/e2e/run.sh` (`docker run --network host` with the Playwright image; `pip install pytest-playwright==0.6.2` inside; env `PANEL_URL`, `PANEL_USER`, `PANEL_PASSWORD`, `PANEL_CARD_ID`).

The test (single): log in, visit every tab (`data-tab` buttons), assert no `pageerror`/`console.error`; switch to RU and assert a known RU string appears; approve card `PANEL_CARD_ID` from the Inbox and assert it disappears from the pending list.

- [ ] Run it against a local agent_api (as in m10) → fix any UI bugs found (each fix gets a unit or HTTP test where possible) → PASS. Commit.

### Task 8: Acceptance, docs, release

- [ ] `infra/tests/m11_flow.sh`:
  1. `platform.sh up` (twice: idempotent).
  2. Assert the Argo app `platform-control` is Healthy, `/healthz` answers on 30400, and 30400 is not bound on a non-loopback IPv4.
  3. e2e (Task 7) with a HITL card created for it.
  4. `project-config put --repo swarm-lab/isv-product --intake`; file an issue labelled `swarm` (a small, deterministic task).
  5. Wait for the daemon to create exactly one run, then `platform.sh restart` once while it is active.
  6. Wait for the run `done` (HITL merge for isv-product goes through auto-merge per its policy), the judge comment on the issue, and the rollout Healthy.
  7. Assert `/runs?task_ref=` returns 1.
- [ ] Run live: PASS. m5 regression.
- [ ] Docs:
  - TUTORIAL §13 "Run the platform" (+ru): `platform.sh up`, the panel, file a `swarm` issue;
  - INSTALL (+ru), README, ARCH (topology), STATE (M11, v1.1.0), TODO.
- [ ] PR → CI → merge → release **v1.1.0** → platform-sync pull → `platform.sh up` from main, so the platform stays running.
