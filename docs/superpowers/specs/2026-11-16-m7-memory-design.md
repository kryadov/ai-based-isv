# M7: project memory — episodic write-back, retrieval, org promotion

Spec §7.4, §8 M7, ADR-0003. The owner said to continue without stopping ("продолжай и не останавливайся до конца"). Every "Ruling" here is my call, open to correction.

## Intent

Today every task starts from zero. M7 closes the loop **experience → next task**:

1. When a vertex closes, what happened is written to **project episodic memory**: deploy verdicts, rollbacks with their postmortems, gate and acceptance failures, and lessons extracted from the session.
2. When a vertex starts, the worker asks for a **memory slice** relevant to the task (same project, overlapping paths, matching words) and puts it into the agent's prompt.
3. A human can **promote** an episode into **org memory**, a git repo in Gitea, through a PR. That repo also carries the **reuse catalog v1**.

**Acceptance (`m7_flow.sh`):**
1. A rollback on `isv-product` (bad canary) writes a postmortem episode.
2. The next agent task on the same project gets that episode in its prompt, as evidence in the session log.
3. A promotion PR to `swarm-lab/org-memory` is opened for the episode.

## Scope

**In scope:**
- episodic store + API;
- worker write-back + retrieval;
- the memory-extractor role (light LLM, fail-open);
- the org-memory repo with the reuse catalog seed;
- the promotion CLI.

**Deferred** (TODO, each a ruling, because they are independent subsystems):
- code indexing;
- pgvector embeddings;
- spec self-evals;
- automatic promotion after ~10 dogfood tasks (ADR-0003 already makes promotion human-only in v0).

## Approaches

1. **Episodes in the control-plane Postgres, lexical + path-overlap ranking (chosen).** The agent_api already owns the run/vertex tables and creates its schema at start.

   Retrieval score: path overlap with the task's `paths_allow` and changed files, plus keyword overlap with the task text, plus a recency boost. The highest-scoring episodes are returned.

   This works on the same DB in tests (SQLite) and on the stand (Postgres).
2. **pgvector embeddings via LiteLLM.** Better semantic recall, but it needs an embedding model through the gateway. The z.ai Coding Plan endpoint serves chat only, and the owner's rule bars OpenRouter except at the plan limit. Rejected for v0; ADR-0003 revisit trigger: "retrieval quality below threshold on the dogfood corpus".
3. **A separate memory service.** Rejected: one more deployment for no v0 gain.

## Design

### 1. Store (`apps/agent_api`)

**Table `memory_episodes`:**
- `id`, `project`, `run_id?`, `vertex_id?`;
- `kind`: one of `lesson`, `deploy`, `postmortem`, `failure`, `decision`;
- `title` (≤200 characters), `body` (text, ≤8000);
- `paths` (JSON list), `tags` (JSON list), `created_at`.

**Endpoints:**
- `POST /memory/episodes` validates `kind`, trims lengths and returns `{id}`.
- `GET /memory/slice?project=&paths=a,b&q=text&limit=5` returns the ranked episodes, each with a `score`.
- `GET /memory/episodes/{id}`.

**Ranking, in `store.rank_episodes`, as a pure function:**
- **Paths:** +3 per path glob match, where an episode path matches a task glob or shares its first two path segments.
- **Words:** +1 per shared word of length 4 or more, title and body, case-insensitive, stop-words dropped, capped at +5.
- **Recency:** +1 for an episode from the last 7 days.
- **Kind:** +2 for a `postmortem` or `failure` (failures are the most valuable to avoid).
- **Filter:** score 0 is dropped. Ties go to the newer episode.

### 2. Write-back (`agent_runtime/memory.py` + worker)

On vertex close the worker posts the episodes below. All posts are fail-open: a memory outage never fails a vertex, it is audited as `memory_skipped`.
- **`deploy`:** the verdict summary (status, image, revision), on a successful deploy. This is the spec's "success marker into memory".
- **`postmortem`:** on a rollback, the body is the postmortem draft.
- **`failure`:** on a terminal fail or exhausted attempts, with the reason codes. Transient requeues are skipped.
- **`lesson`:** produced by the **memory-extractor** on success. It is a light-model LiteLLM call (`qwen-coder`) given the task, the changed files and the review summary, and returns JSON `{"lessons": [{"title", "body"}]}` with at most 2 lessons. Invalid JSON yields no lessons.

  Ruling: the extractor reads the vertex result plus the review summary, not the raw session log, to keep tokens small.

Every episode carries `paths` = the changed files (or `paths_allow` on a failure).

### 3. Retrieval at vertex start

`worker` calls `GET /memory/slice` with the project (from `task_ref` owner/repo, or `repo_url`), `paths_allow` and the vertex name. If anything comes back, `build_vertex_prompt` appends a section:

```
Project memory (lessons from earlier tasks — follow them):
- [postmortem] <title>: <first 400 chars of body>
...
```

The slice is recorded in the session log (`memory_slice`, episode ids) as evidence.

Ruling: prompt injection, not a `.swarm/context.md` file. The sandbox clones the repo itself, and a file would land in the diff.

### 4. Org memory (`swarm-lab/org-memory`)

`bootstrap.sh org-memory` seeds the Gitea repo with:
- `README.md`;
- `lessons/` (empty, with a `.gitkeep`);
- `reuse-catalog.yaml` (v1: a list of `{name, kind, repo, path, description}`), seeded with the platform's reusable pieces: the check profiles, the deploy templates, the isv_product service template;
- the python check profile, so CI runs on PRs; it runs only gitleaks/semgrep, as there is no code to test.

**Promotion:** `org-bootstrap memory promote --episode N` reads the episode from agent_api and opens a PR in `org-memory` adding `lessons/<project>-<id>.md`, with front matter carrying the source run/vertex and kind. It is human-reviewed.

Ruling: promotion PRs are authored as `swarm-admin` (the operator). Agents never write org memory in v0, per ADR-0003.

### 5. Read access for agents

Ruling: no new MCP server in M7. The slice already arrives in the prompt. On-demand search would need its own read-only `memory` MCP server: the task-reader holds a Gitea token and must not grow a control-plane client. That server is in TODO.

## Non-goals

- Embeddings.
- Code indexing.
- Spec self-evals.
- Working memory (the coordinator scratchpad, M8 swarm).
- Automatic promotion.
- A UI (M10).

## Anti-goals

- Memory influencing gates. Lessons go to the agent prompt only, never to merge or deploy decisions.
- A memory outage failing a vertex.
- Agents writing org memory.
