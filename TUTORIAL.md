# Tutorial: using the platform on the lab stand

> Русская версия: [TUTORIAL.ru.md](TUTORIAL.ru.md)

This walks you from "the stand is up" to "an agent shipped my change". Every command here was run on the current stand (Windows + Rancher Desktop + toolbox, AGENTS.md §10). Run all commands **from Git Bash in the repo root**. Anything that needs Python, kubectl tooling or uv goes through `./infra/scripts/toolbox.sh` (on Windows the native `python3` is a Store stub that hangs).

What the platform does, in one line:

**tracker issue → agent in a k3s sandbox → PR → CI (tests + security) → bot review → fix rounds → your HITL approval → merge → issue closed**

How it works inside: [ARCH.md](ARCH.md) §3 (pipeline), §3a (self-development). Rules: [AGENTS.md](AGENTS.md). Current state: [STATE.md](STATE.md).

---

## 1. Check the stand

After a Rancher Desktop restart, OpenBao (secrets) comes up **sealed**. Unseal it first; this is idempotent and safe to repeat:

```bash
./infra/scripts/toolbox.sh bash infra/scripts/bootstrap.sh vault
```

Then check the tools and the core services:

```bash
./infra/scripts/toolbox.sh uv run org-bootstrap check
kubectl -n platform get pods          # gitea, litellm, argocd, prometheus... all Running
```

If something is missing, re-run its section (all sections are idempotent):

```bash
./infra/scripts/toolbox.sh bash infra/scripts/bootstrap.sh <section>
```

The sections are listed in [INSTALL.md](INSTALL.md).

## 2. Secrets and web UIs

All passwords and tokens live in OpenBao. A helper for this shell session:

```bash
bao_get() {  # usage: bao_get <path> <field>
  ./infra/scripts/toolbox.sh bash -c "kubectl -n platform exec \$(kubectl -n platform get pod -l app.kubernetes.io/name=openbao -o name) -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN=\$(cat infra/.vault-root-token) bao kv get -field=$2 $1"
}
bao_get secret/gitea/admin password
```

| What | Where | Login |
|---|---|---|
| Gitea (repos, issues, agent PRs, CI runs) | http://localhost:30080 | `swarm-admin` / `bao_get secret/gitea/admin password` |
| Argo CD | http://localhost:30081 | `admin` / `kubectl -n platform get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' \| base64 -d` |
| Grafana (metrics; Loki logs under Explore) | http://localhost:30082 | `admin` / `bao_get secret/grafana/admin password` |
| LiteLLM (model gateway, OpenAI API) | http://localhost:30084/v1 | Bearer `bao_get secret/litellm/master master-key` |

Model routing: every call goes to the z.ai GLM Coding Plan. OpenRouter is used **only** after z.ai reports its plan limit (AGENTS.md §10). Provider keys go into the repo-root `.env` (template `.env.example`), never into git.

## 3. Watch one task go through end to end (5 minutes)

```bash
./infra/scripts/toolbox.sh bash infra/tests/m5_flow.sh
```

The flow:
1. seeds a tiny Python project, `swarm-lab/target-scratch`;
2. opens an issue in it ("add `reverse_words`");
3. runs an agent (OpenCode + GLM via LiteLLM) in a sandbox pod in namespace `agents`;
4. opens a PR, which goes through CI (`ci / checks`, `ci / security`) and the bot review;
5. approves the HITL merge request itself — **only this demo flow auto-approves** — and the PR is merged;
6. closes the issue.

It ends with `M5 flow: PASS`.

Look at the result in Gitea, in `swarm-lab/target-scratch`:
- the closed issue with `[platform] status:` comments;
- the merged PR, its review comments and its CI run under **Actions**.

The evidence is in `.agent-runtime-workspace/run-<N>/`:
- `worker.log`;
- `.agent_runtime/runs/run-<N>/vertex-<N>/session_log.ndjson`, the full agent event stream;
- `manifest.json`, with the image, model, diff stat and verdict.

## 4. The HITL inbox: you approve merges

Every non-trivial merge waits for a human. While a flow is running, its control plane (`agent_api`) listens on port 8472 inside the toolbox network:

```bash
./infra/scripts/toolbox.sh curl -s http://127.0.0.1:8472/hitl/pending
./infra/scripts/toolbox.sh curl -s -X POST http://127.0.0.1:8472/hitl/<id>/decision \
  -H 'Content-Type: application/json' -d '{"action":"approve","decided_by":"<your name>"}'
```

Use `"action":"reject"` to refuse. Read the PR diff, its CI and the bot review in Gitea before you decide. A CLI command and a web panel are on the roadmap (TODO.md).

## 5. Your own project

### 5.1 Create the repo with the CI corridor

```bash
./infra/scripts/toolbox.sh uv run org-bootstrap onboard --project my-svc --stack python --push-whitelist swarm-admin
```

Stacks (check profiles):
- `python`;
- `typescript` (M9: npm ci, tsc, eslint, node:test);
- `docs` (no code).

Start from a code template with `--template`:

```bash
./infra/scripts/toolbox.sh uv run org-bootstrap onboard --project ts-greeter --stack typescript --template ts-service
./infra/scripts/toolbox.sh bash infra/tests/m9_flow.sh   # TypeScript dogfood: template → agent change → corridor
```

For a TypeScript task, the acceptance command runs node in docker:

```bash
SELFDEV_DONE_CMD="docker run --rm -v \"\$PWD\":/w -w /w node:22.12.0-alpine sh -c 'npm ci && npm test'"
```

This:
- creates `swarm-lab/my-svc`;
- seeds the check profile (`.gitea/workflows/ci.yml`: ruff, mypy, pytest, gitleaks, semgrep) and `.platform/merge-policy.yaml`;
- protects `main`: merges only with green CI;
- with `--push-whitelist swarm-admin`, lets you push the initial code directly;
- registers the project in `infra/projects.yaml`.

Agents work as the non-admin `swarm-agent`, which can write to every `swarm-lab` repo.

### 5.2 Push your code

Your code needs a `pyproject.toml` and pytest tests: the python profile runs `uv sync --all-groups`, then ruff, mypy and pytest.

```bash
git clone http://localhost:30080/swarm-lab/my-svc.git && cd my-svc
# add code + tests, then:
git add . && git commit -m "initial code" && git push   # user swarm-admin, password from bao_get
```

### 5.3 Give the agent a task

The self-development flow takes any `swarm-lab` repo through `SELFDEV_REPO`:

```bash
SELFDEV_REPO=my-svc \
SELFDEV_TITLE="Add /version endpoint" \
SELFDEV_BODY="Add GET /version returning {\"version\": \"0.1.0\"} in src/my_svc/app.py, with a pytest test in tests/test_version.py." \
SELFDEV_VERTEX="add GET /version endpoint returning the package version; test in tests/test_version.py" \
SELFDEV_DONE_CMD="uv sync --quiet --all-groups && uv run --quiet pytest -q" \
SELFDEV_PATHS="src/**,tests/**" \
./infra/scripts/toolbox.sh bash infra/tests/selfdev_flow.sh
```

What the variables mean:
- `SELFDEV_VERTEX` is the short task line, at most 120 characters;
- `SELFDEV_BODY` is the full text: the issue the agent reads;
- `SELFDEV_DONE_CMD` is the acceptance check, run on the host before the PR is opened;
- `SELFDEV_PATHS` lists the files the agent may touch.

When the flow prints `HITL merge request #<id> PENDING`, review the PR in Gitea and approve it (§4). The flow then waits for the merge and ends with `Selfdev flow: PASS`.

This exact path is verified on the platform repo itself. For new projects it is the same code path.

## 6. A task for the platform itself (self-development)

The platform develops itself through the same corridor (AGENTS.md §11). GitHub stays canonical; Gitea `swarm-lab/ai-based-isv` is the agents' copy.

```bash
bash infra/scripts/platform-sync.sh pull        # host Git Bash: fast-forward Gitea from GitHub
SELFDEV_TITLE=... SELFDEV_BODY=... SELFDEV_VERTEX=... SELFDEV_PATHS=... \
  ./infra/scripts/toolbox.sh bash infra/tests/selfdev_flow.sh
# approve the HITL request (§4) after reading the PR
bash infra/scripts/platform-sync.sh promote     # opens a GitHub PR selfdev/<sha7>
gh pr merge <N> --merge                         # after the GitHub CI is green
bash infra/scripts/platform-sync.sh pull
```

The first platform change made this way: Gitea PR #3 → GitHub PR #29 (release v0.6.0).

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| Anything touching secrets fails after a restart | `./infra/scripts/toolbox.sh bash infra/scripts/bootstrap.sh vault` (unseal) |
| `python3` hangs on Windows | it is the Store stub — use `./infra/scripts/toolbox.sh ...` |
| `something already listens on port 8472` | a previous flow is still running, or set `M4C_API_PORT=<free port>` |
| Flow fails with `done_when ... exited 2` | the acceptance command could not collect tests (e.g. workspace not synced): fix `SELFDEV_DONE_CMD` |
| `platform-sync.sh pull` exits 3 (`unpromoted`) | Gitea has merged work not yet on GitHub — run `promote` first |
| Model calls fail | check `.env` keys → `bootstrap.sh litellm`; z.ai errors other than its limit are not rerouted to OpenRouter by design |

## 8. Deploy to prod: canary with metric analysis (M6)

The demo service `swarm-lab/isv-product` (seeded by `bootstrap.sh demo`) runs in namespace `prod` as an **Argo Rollouts canary**, managed by Argo CD from the repo's `deploy/` directory.

- Traffic: k6 sends a steady 10 rps.
- Metrics: Prometheus (http://localhost:30090) and Grafana.

When the corridor merges a change and deploy is enabled, the platform deployer:
1. builds `isv-product:<sha7>` from the merged `main`;
2. measures the pre-rollout baseline (5xx ratio, p95);
3. points the Argo CD Application at the new image.

Argo Rollouts then shifts traffic 20 → 50 → 100 % while analysis runs:
- **canary vs stable:** the canary must carry traffic, its 5xx ratio ≤ max(2 × stable, 5 %), its p95 ≤ max(1.5 × stable, 300 ms);
- **post-rollout window:** the whole service against the baseline.

If an analysis fails, the rollout aborts, prod returns to the stable version, and the evidence gets a `postmortem.md` draft. A rollout without a passed analysis never counts as successful.

```bash
./infra/scripts/toolbox.sh bash infra/tests/m6_flow.sh   # good path (agent change) + bad path (injected fault → rollback)
kubectl -n prod get rollout isv-product                  # phase, step
kubectl -n prod get analysisrun                          # analysis verdicts
```

Deploy a commit by hand, or show a rollback with an injected fault:

```bash
./infra/scripts/toolbox.sh uv run python -m agent_runtime.deployer --sha <sha> --repo-dir <clone> \
  [--fault FAULT_ERROR_RATE=0.5] --evidence-dir /tmp/deploy
```

**Deploy config is human-only.** `deploy/**` is a protected gate path: agents cannot change it. Change it with an operator PR in Gitea. The first prod rollout of a service also needs a HITL approval (gate #3).

Agents see rollouts and metrics through the read-only `observability` MCP server (`get_rollout`, `service_slo`, `query_metric`).

## 9. Project memory (M7)

Every finished task leaves a trace in **project episodic memory**:
- deploy verdicts;
- rollback postmortems;
- terminal failures;
- up to two lessons written by the light-model **memory-extractor**.

The next task on the same project gets the most relevant episodes (by overlapping paths and shared words) appended to its prompt, so agents do not repeat a rolled-back mistake. The episode ids are recorded in the session log (`memory_slice`).

```bash
./infra/scripts/toolbox.sh bash infra/tests/m7_flow.sh      # rollback → postmortem → next task sees it → promotion PR
curl -s "http://127.0.0.1:8472/memory/slice?project=swarm-lab/isv-product&paths=src/**&q=canary"   # while a flow runs
```

**Org memory** is the Gitea repo `swarm-lab/org-memory`: `lessons/` plus `reuse-catalog.yaml`, the catalog of components to reuse. A human promotes a useful episode with:

```bash
./infra/scripts/toolbox.sh uv run org-bootstrap memory promote --episode <id> --api http://127.0.0.1:8472
```

This opens a PR in org memory for review. Agents never write org memory.

## 10. Swarm mode: one task, several agents in parallel (M8)

A larger task is split by the **coordinator** into a vertex graph. Independent vertices run **in parallel** on several workers; dependent ones start once their deps are merged and get a **handoff** (files, PR, review of each dep). Every vertex goes through the corridor as its own PR. At the end the **judge** checks that the merged `main` passes the done command.

```bash
./infra/scripts/toolbox.sh bash infra/tests/m8_flow.sh   # issue → plan → 3 workers → corridor ×N → judge
```

Parts, all on the same worker env as `selfdev_flow.sh`:

```bash
uv run python -m agent_runtime.swarm plan --run-id N --title "..." --body "..." \
  --allowed "src/**,tests/**,docs/**" --done-cmd "uv sync --quiet --all-groups && uv run --quiet pytest -q"
uv run python -m agent_runtime.swarm run --workers 3          # until every vertex is done/failed
uv run python -m agent_runtime.swarm judge --run-id N --done-cmd "..." --project swarm-lab/<repo>
curl -s http://127.0.0.1:8472/metrics                          # swarm_vertices / swarm_hitl_pending / swarm_runs
```

Guarantees:
- The planner cannot widen scope or run commands.
- Parallel vertices never share files.
- A bad plan degrades to a single vertex.
- Workers pause taking new work while merges wait for you (backpressure, default 3).

## 11. The organization panel and Automations (M10)

> Since M11 the panel runs in-cluster at http://localhost:30400/ui: start it with `platform.sh up` (§12). The manual way below is for development.

The panel is served by the control plane at `/ui`. It is for humans only; agents are service identities and never log in. Enable it with local users (OIDC is planned):

```bash
PW_HASH=$(printf '%s' 'your-password' | sha256sum | cut -d' ' -f1)
AGENT_API_UI_USERS="you:$PW_HASH:admin" AGENT_API_SESSION_SECRET=$(openssl rand -hex 24) \
  ./infra/scripts/toolbox.sh uv run uvicorn --factory agent_api.main:create_app --host 127.0.0.1 --port 8472
# open http://127.0.0.1:8472/ui   (from the host: run the toolbox with the port reachable, or use the flows' agent_api)
```

Without configured users the panel is disabled (503), never open.

| Tab | What it shows |
|---|---|
| **Inbox** | Decisions waiting for you: merge, deploy, escalation, automation. Approve or reject them; the decision is recorded under your name. The health strip at the bottom shows pending decisions, finished and failed runs, running vertices and memory episodes. |
| **Projects** | The registry (`infra/projects.yaml`) and runs per repo. |
| **Swarm** | Runs and their vertex graphs: status, deps, agent, PR. |
| **Automations** | Definitions (`infra/automations/*.yaml`), "Run now", run history with findings and created issues. |
| **Metrics** | Control-plane gauges (`/metrics`). |
| **Memory** | Search over project episodes, with the promotion command. |
| **Pipeline, Settings** | Placeholders (planned). |

EN/RU toggle in the top bar.

**Automations.** A requested run is executed by the engine on the host:

```bash
AGENT_RUNTIME_API_URL=http://127.0.0.1:8472 AUTOMATIONS_GITEA_TOKEN=$(bao_get secret/gitea/admin token) \
  ./infra/scripts/toolbox.sh uv run python -m agent_runtime.automations poll   # or: run vuln-scan
./infra/scripts/toolbox.sh bash infra/tests/m10_flow.sh                         # login → Inbox decision → vuln-scan → fix task
```

`vuln-scan` runs semgrep (configs from the definition) and gitleaks over its scope and files an Inbox card with the findings. Approving the card creates one `[vuln-scan]` fix task per affected repo. A rejection does nothing.

## 12. Run the platform here: a standing service (M11)

Until M11, the platform worked only while an acceptance script ran. Now it runs as a service on the stand.

```bash
bash infra/scripts/platform.sh up          # host Git Bash; idempotent; default ref: origin/main
bash infra/scripts/platform.sh status      # pin, daemon, panel, Argo app
bash infra/scripts/platform.sh password    # the panel password of user `owner` (prints it)
bash infra/scripts/platform.sh logs 100    # the daemon: dispatcher + automation poller
bash infra/scripts/platform.sh restart | down
```

`up` does three things:
1. **Pins the code.** It creates the git worktree `.platform-live` at the ref. Editing your dev tree never changes the running platform; run `up` again to move the pin.
2. **Deploys the control plane.** It builds the image `swarm-platform:<sha7>` and deploys agent_api plus the panel as the Argo CD app **`platform-control`** (namespace `platform`).
3. **Starts the daemon.** The container `swarm-platform-daemon` runs with `--restart unless-stopped`, so it survives closed terminals and Rancher restarts.

**The panel:** http://localhost:30400/ui, user `owner`. Every service endpoint on :30400 needs a bearer token. Each identity has its own token in OpenBao `secret/platform/api` (M12): `daemon`, `worker`, `poller`, and `token` for the operator (you, the acceptance flows). The cluster stores only their sha256 hashes. Only the operator may decide a HITL request over the API (a worker gets 403), and audit rows record the calling identity. Rotate with `./infra/scripts/toolbox.sh bash infra/scripts/bootstrap.sh platform-token rotate [daemon|worker|poller|token|all]`, which restarts the control plane and the daemon. A logged-in panel session may *read* the listings the panel shows; every action goes through the panel's own `/ui/api`, recorded under your name.

**Give the platform a task**
1. The repo needs `.platform/project.yaml` with `intake: true`, which `onboard` seeds from the check profile:
   ```yaml
   intake: true
   paths: ["src/**", "tests/**"]                 # what agents may change
   done_when: "uv sync … && uv run pytest -q"    # the platform's acceptance command
   deploy: {app: isv-product, namespace: prod}   # optional: canary after merge
   workers_per_run: 3
   ```
2. File an issue there with the label **`swarm`**.
3. Within ~30 s the dispatcher picks it up:
   - creates a run, plans the vertex graph, starts agents and comments the plan on the issue;
   - merge decisions appear in the panel **Inbox**;
   - when every vertex is done, it judges merged `main` against `done_when` and comments the verdict.
4. One issue gets one run. **To retry a failed task** (M12), add the label **`swarm-retry`** to the issue, or press **Retry** on the failed run in the panel's Swarm tab (admin role). Either starts a new run when the task's newest run failed; on a done or still-running task the label is removed with a comment.

**Limits:**
- 2 active runs and 11 sandboxes (the NodePort pool) at a time; extra issues wait.
- The platform repo itself is opt-in (`intake: false` in its `.platform/project.yaml`); its merges stay HITL either way.
- Restarting the daemon is safe (M12): workers get SIGTERM, give their vertex back at once (the attempt is not counted) and tear their sandbox down; the container has a 120 s stop timeout. The next tick re-spawns workers for the released vertices.
- After a Rancher Desktop restart OpenBao comes up sealed. The panel keeps working, but the daemon restarts until it can read its secrets: run `platform.sh up`, which unseals OpenBao first.

**Updating gate files.** `onboard` and `seed-demo` update the profile files in an already protected repo through **one PR** (`platform/profile-update`), merged after CI.

**Cron automations.** A definition with `trigger: cron` and `schedule: "<m h dom mon dow>"` (UTC) gets a run requested by the daemon's poller when a tick passes. A tick missed while the daemon was down is caught up once. `vuln-scan` runs on Mondays at 06:00 UTC; its findings still wait for your approval in the Inbox.

**Acceptance:**
```bash
bash infra/scripts/platform.sh up --ref <ref>
./infra/scripts/toolbox.sh bash infra/tests/m11_flow.sh   # e2e in Chromium + issue → merged → canary, no flow help
```

The browser e2e alone: `PANEL_URL=… PANEL_USER=owner PANEL_PASSWORD=… PANEL_CARD_ID=… bash infra/tests/e2e/run.sh`. Against a throwaway local agent_api (what GitHub CI runs): `bash infra/tests/e2e/ci.sh`.

> ⚠ **Keep the stand's NodePorts off your LAN (M12).** Rancher Desktop forwards them on `0.0.0.0`, and its installer lets `host-switch.exe` in on Private/Domain networks. Run once, in an **elevated** PowerShell at the repo root:
> ```powershell
> powershell -ExecutionPolicy Bypass -File infra\rancher\restrict-firewall.ps1          # loopback only
> powershell -ExecutionPolicy Bypass -File infra\rancher\restrict-firewall.ps1 -Check   # no admin needed
> ```
> `platform.sh status` prints the result as `lan: …` (a Rancher upgrade can recreate the rules; `-Revert` restores them). Defence in depth: the task-source MCP (:30085) also requires its bearer token (OpenBao `secret/mcp/task-source`). Prometheus (:30090) relies on the firewall.

## 13. What is not there yet

- OIDC/SSO.
- The Pipeline kanban.
- The other catalogue automations (dep-updates, log-analysis, feedback-ingest, …).
- Scoped LiteLLM virtual keys per run (sandboxes still use the master key).
- Embeddings and code indexing.
- Baseline invalidation.
- Workers on several hosts.

See [TODO.md](TODO.md).

See [STATE.md](STATE.md) and [TODO.md](TODO.md).
