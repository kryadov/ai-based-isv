# M12 — stand hardening and run lifecycle (design)

Date: 2026-11-23. Source: TODO "M11 follow-ups" plus the related M4c security minors.
The owner asked to work through the backlog and to make the calls without asking
("I care about the result"); the one owner decision taken: **LAN exposure is closed both
ways — the firewall rules are restricted AND the task-source MCP gets a bearer token.**

## Intent

The platform now runs as a standing service (M11). What stands between it and unattended
operation: services reachable from the LAN without auth, one shared service token, no way to
retry a failed task, workers that die mid-session on a daemon restart, a panel e2e that only
runs by hand, and a stale Gitea branch. M12 closes these. Baseline invalidation (spec §7.3) is
a coordinator feature, not hardening: it moves to M13.

## Goals

1. **LAN exposure closed.**
   - `infra/rancher/restrict-firewall.ps1`: limits the inbound "Rancher Desktop Networking …"
     allow rules to `RemoteAddress = 127.0.0.1, ::1` (loopback-only; host-switch talks to the VM
     over hvsocket, not over inbound IP). Idempotent, `-Revert` restores `Any`, prints before/after.
     It needs an elevated PowerShell, so the owner runs it once (one command in INSTALL/STATE).
   - task-source MCP requires `Authorization: Bearer <token>` when `MCP_AUTH_TOKEN` is set
     (ASGI middleware around the FastMCP HTTP app; constant-time compare; 401 otherwise).
     Token in OpenBao `secret/mcp/task-source` (field `token`, generated once by `section_mcp`),
     into Secret `mcp-task-source` key `auth-token`; the daemon passes it to workers as
     `AGENT_RUNTIME_TASK_SOURCE_TOKEN`; `TrackerClient` sends it. The sandbox-facing
     task-reader/llm/ci/observability stay NetworkPolicy-bounded (ClusterIP only, no NodePort).
   - Prometheus :30090 has no auth and stays covered by the firewall only (owner choice).
2. **Per-identity service tokens + rotation.** OpenBao `secret/platform/api` keeps field
   `token` (now the `operator` identity: flows, humans' CLI) and gains `daemon`, `worker`,
   `poller`. agent_api reads `AGENT_API_SERVICE_TOKENS` = `name:sha256hex,...` (hashes only, so
   the Deployment's Secret never holds a usable token); the legacy single `service_token` still
   works as identity `operator`. The matched identity is put on `request.state.identity`;
   `POST /hitl/{id}/decision` is allowed only for `operator` (a worker or the daemon can never
   approve its own merge); `/audit` rows record `identity` in the payload.
   `bootstrap.sh platform-token rotate [daemon|worker|poller|operator|all]` writes fresh tokens,
   re-renders Secret `agent-api`, restarts the Deployment; `platform.sh restart` picks them up.
3. **Retry of a failed task.** Two entry points, one mechanism (a new pending, vertex-less run
   for the same `task_ref`, which the dispatcher already plans on its next tick):
   - label `swarm-retry` on the open issue: if the newest run of the task is `failed`, the
     dispatcher creates the run, removes the label and comments; if it is `done` or still
     `pending`, it removes the label and comments why not;
   - panel: `POST /ui/api/runs/{id}/retry` (admin role) — same rule; a Retry button on failed
     runs in the Swarm tab; decided under the human's name in the audit.
   The label is created by the check profile next to `swarm`.
4. **Graceful worker shutdown.** The worker handles SIGTERM by setting its stop event. A vertex
   in flight is **released**: new `POST /vertices/{id}/release` (lease holder only) puts it back
   to `queued`, clears the lease and gives the attempt back (`attempts - 1`, floor 0) — a
   restart is not a failure. The session is stopped (sandbox pod torn down) in `finally`. The
   daemon on SIGTERM terminates workers and waits up to `shutdown_grace_s` (default 60) before
   killing; `platform.sh` starts the container with `--stop-timeout 120`.
5. **Browser e2e in CI.** `infra/tests/e2e/ci.sh` starts agent_api on SQLite with a test panel
   user, seeds one HITL card and runs `test_panel.py`; a new `e2e` job in
   `.github/workflows/ci.yml` installs Playwright Chromium and runs it.
6. **Stale `platform-live` branch** removed from Gitea once its commit is on `main`, and
   `section_platform` deletes it whenever the pinned commit is already on `main`.
7. **Secrets off command lines** in `section_mcp`, `section_litellm`, `section_platform` and
   `m4c_flow.sh`: Secrets are rendered by a bash-builtin `printf` into `kubectl apply -f -`
   (helper `apply_secret`), never via `--from-literal`.

## Non-goals

- Baseline invalidation / coordinator reset (M13).
- OIDC for the panel, scoped LiteLLM virtual keys per run (later increments).
- Auth on Prometheus.

## Approaches considered

- *MCP auth via FastMCP's auth providers (OAuth/JWT)* — heavy for a host-only service with one
  caller; a static bearer in ASGI middleware matches agent_api's M11 scheme. **Chosen: static
  bearer.**
- *Per-identity tokens as plaintext env* vs *hashes in env* — hashes cost nothing and keep the
  Deployment spec harmless. **Chosen: hashes.**
- *Retry by re-queuing the old run's vertices* vs *a new run* — a new run keeps history and
  re-plans against the current `main`. **Chosen: new run.**
- *Shutdown: fail the vertex* vs *release it* — failing burns an attempt and can blacklist
  a vertex after 2 restarts. **Chosen: release.**

## Verification

Unit tests (TDD) for every piece; gates `ruff`, `mypy --strict`, `pytest`; live: rebuild MCP
(`bootstrap.sh mcp`), `platform.sh up` from the branch, check 401 without token on :30085 and
:30400, `m5_flow` (worker/store change), `m11_flow`; the e2e job green on GitHub CI.
