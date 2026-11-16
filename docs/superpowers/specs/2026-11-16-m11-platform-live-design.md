# M11: the platform runs live on the stand

The owner's directive is to finish the platform and run it here ("надо дожать платформу и запустить ее прямо здесь"); the standing rule is to work autonomously. Every **Ruling** below is my call and open to correction.

## Intent

Today the platform works only while an acceptance script runs:
- a flow starts `agent_api` on the host;
- it polls the tracker inline, creates the run and the vertex itself;
- it starts a run-scoped worker and tears everything down at exit.

Nobody can just file a task and have the platform pick it up. M11 makes the platform a **standing service**:
- a human files an issue in an onboarded repo and labels it `swarm`;
- the platform plans the task, runs agents, opens PRs and walks the corridor;
- decisions show up in the panel, which is always reachable;
- automations fire on their schedules;
- everything survives a terminal closing and restarts with Rancher Desktop.

**Acceptance (`m11_flow.sh`):**
1. `platform.sh up` brings the platform up idempotently. The control plane is in-cluster (Argo CD app Healthy); the daemon container is running.
2. The panel is reachable at `http://localhost:30400/ui` with the owner's password from OpenBao. A **browser e2e** test (Playwright) covers:
   - login;
   - every tab renders with no JS errors;
   - the EN/RU switch;
   - a card decided from the Inbox.
3. An issue labelled `swarm` in `swarm-lab/isv-product` becomes a merged change with no flow-side help, deployed by canary. The dispatcher creates the run and the plan, spawns workers and judges the run; the issue gets the result comment. A second dispatcher poll creates no second run.
4. `platform.sh restart` in the middle leaves no duplicate runs. Active runs resume: a dead worker's leases expire and new workers reclaim them.

## Approaches

| | Approach | Trade-off |
|---|---|---|
| **A (picked)** | **Control plane in-cluster, executors as a host daemon container.** `agent_api` and the panel run as an Argo CD app. The dispatcher, its workers and the automation scheduler run in one long-lived toolbox-image container (`docker run -d --restart unless-stopped`, host network, docker socket, kubeconfig). | The workers keep their proven host-side shape: docker builds for deploys, `done_when` in docker, sandbox NodePorts on localhost. Only the control plane moves. |
| B | Everything in-cluster: workers as a Deployment with a hostPath docker socket and in-cluster sandbox addressing. | Rewrites the sandbox addressing and the deploy path that M4–M9 proved live; much risk for no user-visible gain on a one-host stand. |
| C | Everything as host containers (compose), no Argo CD. | Simplest, but the control plane would leave GitOps, health checks and Prometheus scraping. |

## Rulings

### Control plane in the cluster
- Image: `swarm-platform:<sha7>` (`infra/platform/Dockerfile`), built on the moby engine that k3s shares (no registry, as for isv-product).
- Kustomize lives in `infra/platform/control-plane/`: Deployment `agent-api` in namespace `platform`, Service NodePort **30400**, ServiceMonitor for `/metrics`.
- Argo CD app `platform-control` comes from Gitea `swarm-lab/ai-based-isv` (synced from GitHub). `platform.sh up` sets the image override.
- Secrets come from OpenBao into k8s Secret `agent-api`: the DB URL (Postgres `swarm` database), `AGENT_API_SESSION_SECRET` and `AGENT_API_UI_USERS`.
- The owner's panel user `owner` gets a random password at `secret/platform/ui`; `platform.sh password` prints it on demand, and only then.
- **Exposure:** the service JSON API has no auth yet (service identities on the host). Rancher Desktop forwards NodePorts to `127.0.0.1` only, and the acceptance asserts that 30400 is not on a non-loopback address. A token for the service API is TODO.

### Daemon on the host
- `infra/scripts/platform.sh up|down|restart|status|logs|password` runs a container `swarm-platform-daemon` from the toolbox image.
- It runs on a **pinned checkout**: a git worktree `.platform-live/` at the deployed sha (gitignored). Editing the dev tree never changes the running platform; `platform.sh up` updates the pin.
- The container runs `python -m agent_runtime.daemon`, which supervises two loops:
  - the **dispatcher**;
  - the **automation scheduler/poller**.

### Dispatcher (`agent_runtime/dispatcher.py`)
- Every 30 s it lists the repos of org `swarm-lab` that have a `.platform/project.yaml` with `intake: true`.
- In each such repo it lists open issues with label `swarm`. A `task_ref` that already has a run is skipped; `GET /runs?task_ref=` provides the dedup.
- For a new `task_ref`:
  1. create the run;
  2. `planner` (the existing M8 path) with `title`/`body` from the issue and `paths` + `done_when` from `project.yaml`;
  3. spawn `min(vertices, workers_per_run=3)` worker subprocesses with the run's env (the selfdev_flow env, built in code);
  4. comment the plan on the issue.
- Caps: `max_active_runs` = 2 and at most 11 sandboxes in total (the NodePort pool). Extra tasks wait as `queued`.
- When all vertices are terminal it runs `swarm judge` and comments the verdict. Whether the issue gets closed is the PR's job ("Closes #N" via the tracker loop).
- **Restart:** on startup it adopts non-terminal runs and spawns workers for them; expired leases are reclaimed.
- Ruling: one dispatcher, the single host. Multi-host is TODO.

### Per-project config `.platform/project.yaml`
```
intake: true|false
paths: [...]
done_when: <cmd>
deploy: {app, namespace}?
workers_per_run?
```
- `.platform/**` is already a protected path, so agents cannot change their own gate.
- `onboard` writes it from the check profile:
  - python: `uv sync … && uv run pytest`;
  - typescript: the docker-node command from m9.
- Existing repos (isv-product, the ts demo, the platform repo with `intake: false` by default) get one through `bootstrap` (the platform repo through this GitHub PR).
- Ruling: the platform repo is opt-in. Self-dev keeps HITL on every merge either way.

### Cron
- The scheduler inside the daemon evaluates `trigger: cron` definitions with `croniter` (pinned) and requests a run (`POST /automations/runs`) when a tick passed since the last run of that name. The poller then executes it as today.
- Ruling: not a k8s CronJob. The executor lives on the host anyway; a CronJob would only `curl`, and a second scheduler place would split the logic.
- `vuln-scan` gets `schedule: "0 6 * * 1"` (weekly) and its scope extended to the onboarded repos.

### Browser e2e
- `infra/tests/e2e/test_panel.py` runs Playwright in docker `mcr.microsoft.com/playwright/python:v1.49.0-noble` (pinned) against the live panel.
- It fails on any `console.error` or `pageerror`.
- Run from `m11_flow.sh`; in CI: TODO.

## Non-goals

- Baseline invalidation and coordinator reset (spec §7.3): the next increment.
- OIDC.
- Workers on several hosts.
- A service-API token.
- A registry.
- A Pipeline kanban.

## Anti-goals

- A task picked up twice.
- The running platform silently changing when the dev tree is edited.
- The panel or the API reachable from the LAN.
- A daemon that dies with the terminal.
- Secrets in logs. The panel password is printed only by an explicit `platform.sh password`.
