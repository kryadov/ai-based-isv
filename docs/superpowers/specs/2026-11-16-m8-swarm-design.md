# M8: swarm mode — planned vertex graph, parallel executors, judge, backpressure

Spec §7.2 item 3, §7.3, §8 M8 and §14. Target parallelism is 3–5 concurrent vertices. The owner said to continue without stopping. Every "Ruling" here is my call, open to correction.

## Intent

Today one task becomes one vertex and one agent. M8 makes a task a **graph**:
1. a coordinator/planner splits the task into vertices with `deps` and `paths_allow`;
2. several executors work on independent vertices **in parallel**, one writer per path area;
3. each vertex goes through the corridor and lands as its own PR;
4. dependents start once their deps are done and receive a structured **handoff**;
5. a **judge** checks the whole run at the end.

The orchestrator throttles new work when merges pile up (**backpressure**). The control plane exposes **metrics**.

**Acceptance (`m8_flow.sh`):** one issue on `isv-product` asks for three independent utility modules plus a doc page that lists them.
- The planner produces 4 vertices: 3 parallel, plus 1 depending on them.
- 3 workers execute them; at least 2 vertices run concurrently (overlapping leases).
- Every vertex is merged through the corridor.
- The dependent vertex's prompt carries the handoffs of its deps.
- The judge passes the run on the merged `main`.

## Constraints

- The control plane already provides leases (claim with TTL/heartbeat), `deps` (a vertex is claimable once its deps are `done`), per-vertex sandboxes (pods named by run/vertex) and a NodePort pool (30100–30110). M8 builds on these and does not replace them.
- **Workspaces:** the worker workspace is per run (`workspace/run-N`). Parallel workers need separate workspace roots.
- **Merge order:** PRs merge independently. Parallel vertices must not touch the same files, otherwise a later PR conflicts.
- **Safety:** the planner is an LLM. It never emits shell commands (the `done_when` command is platform-supplied) and never widens paths beyond the task's allowed area.
- **LLM:** z.ai first; the planner uses the agent model `qwen3`.

## Approaches

1. **Planner → vertices in the existing control plane; N worker processes on the same run (chosen).** This uses the lease queue exactly as designed in §7.3: workers are interchangeable and horizontal scaling means more workers.
2. **A coordinator process that spawns one sandbox per vertex directly.** Rejected: it duplicates the lease/heartbeat/retry machinery.
3. **Sequential execution of a planned graph.** Rejected: it is not a swarm, and parallelism is M8's point.

## Design

### 1. Control plane additions (`apps/agent_api`)

- `GET /runs/{id}/vertices` returns `[{id, name, status, deps, paths_allow, attempts, leased_by, result}]`: the handoff source and the judge input.
- `POST /runs/{id}/status` with `{"status": "done"|"failed", "note"}` sets the run verdict (judge) and audits it.
- `GET /metrics` returns Prometheus text:
  - `swarm_vertices{status}` (gauge, all runs);
  - `swarm_hitl_pending{kind}` (gauge);
  - `swarm_runs{status}` (gauge).

### 2. Planner (coordinator role, `agent_runtime/planner.py`)

`plan_task(http, *, base_url, api_key, model, title, body, allowed: list[str], max_vertices=5) -> list[VertexPlan]`, where `VertexPlan(name ≤120 characters, deps: list[str], paths_allow: list[str])`. The LLM answers JSON `{"vertices": [...]}`.

`validate_plan(plans, allowed)` enforces:
- names are unique;
- deps reference known names;
- the graph is acyclic;
- 1..max vertices;
- every `paths_allow` is inside `allowed`, by glob containment;
- **vertices that can run concurrently** (neither depends on the other, transitively) have **disjoint** `paths_allow`.

A violation raises `PlanError`. Any planner failure falls back to a single vertex covering the whole task, so the swarm degrades to M7 behaviour instead of failing.

Ruling on disjointness: two globs overlap if either matches the other's literal prefix, or one is a prefix of the other. It is conservative: false "overlap" only serializes work.

### 3. Handoffs (working memory, minimal)

When a vertex is claimed, the worker fetches `GET /runs/{id}/vertices` and, for each dep, adds a prompt section:

```
Handoff from upstream vertices (already merged into main):
- <dep name>: files <…>; PR <url>
```

Ruling: the structured handoff is the deps' result records (files, PR, review summary). A separate working-memory scratchpad table is deferred (TODO): the per-run vertex results already are the run's working memory.

### 4. Parallel executors (`agent_runtime/swarm.py`)

`python -m agent_runtime.swarm run --workers 3` (env as for the worker) spawns N worker processes. Each gets:
- `AGENT_RUNTIME_AGENT=<agent>-w<i>`, a distinct lease identity;
- `AGENT_RUNTIME_WORKSPACE_DIR=<root>/w<i>`, isolated workspaces.

`run` supervises the processes until every vertex of the run is terminal (`done`/`failed`) or a timeout. Workers exit on SIGTERM.

### 5. Backpressure

Before claiming, a worker reads `GET /hitl/pending`. If at least `max_pending_merges` merge requests are pending (default 3, setting `AGENT_RUNTIME_MAX_PENDING_MERGES`), it waits a poll interval instead of claiming. The flow is bounded by review throughput (§7.3 merge queue + backpressure). The waits are audited (`backpressure`).

### 6. Judge (`swarm judge`)

`python -m agent_runtime.swarm judge --run-id N --repo-url … --done-cmd …`:
1. all vertices of the run must be `done`;
2. the merged `main` (fresh clone) must pass the run-level `done_cmd`.

Then it sets the run status `done` and writes a `decision` episode to project memory ("run N judged done: K vertices"). Otherwise it sets `failed` with the reasons. Its exit code reflects the verdict.

### 7. `swarm plan`

`python -m agent_runtime.swarm plan --run-id N --title … --body … --allowed src/**,tests/**,docs/** --done-cmd …` runs the planner and validation (with fallback), then posts the vertices. Every vertex gets `done_when = {"checks": [{"type": "cmd", "run": <done-cmd>}]}`. It prints the graph.

## Non-goals

- Baseline invalidation and reset (TODO).
- Specialist consult roles (architecture/security reviewers beyond the bot reviewer).
- Cross-host workers.
- A merge-queue rebase bot.
- A UI (M10).

## Anti-goals

- Two concurrent writers on the same files.
- A planner-supplied command executed on the host.
- A swarm that fails where a single vertex would have succeeded: the planner falls back to one vertex.
