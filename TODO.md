# TODO

> Актуальное состояние и следующий шаг — в [STATE.md](STATE.md); этот файл — бэклог и обязательные петли.

## Tracker task-status feedback loop (mandatory)

- [ ] Close the feedback loop: the platform must change the tracker task status as agents work.
  - Task picked up by an agent → status **In Progress** (automatically, via the MCP task-source).
  - Key pipeline milestones → task comments: spec ready (link), PR created (link), acceptance passed, rollout done.
  - Completion → **Done** + links to PR and evidence pack. Failure/block → **Blocked** with a reason.
  - Rule: an agent never leaves a task in its original status — the feedback loop is closed in both directions (tracker → platform → tracker).
  - Implementation: callbacks via the MCP task-source server (OpenProject/Jira adapter, spec §7.1), milestone M3/M5.

## Other

- [ ] **Per-kind scaffolds + tutorials** (spec §13.1.5): bootstrapper repo templates per project kind (product/service/tool/assistant — code scaffold, CI, evals+prompt VET for assistants) + step-by-step tutorials `docs/tutorials/onboard-<kind>.md`. Templates: M4+ (when the executor builds), tutorials right after.
- [ ] Docs language sync: README/INSTALL/TODO are EN-base ✓ with `.ru.md` mirrors; the design docs (spec, milestone plans) are still Russian — translate them to EN with `.ru.md` mirrors as backlog.
- [ ] OpenProject password/basic-auth login broken (v17-slim): rails `check_password?` = true, but web login 422 and API basic 401. Investigate (session store/memcached? Enterprise flags?) or add an OIDC plugin; until then the v0 tracker is Gitea Issues (TaskSource poller → M4 MCP task-source).
- [x] ~~Vault dev-mode → постоянный backend~~ ✅ 2026-11-08: **OpenBao** (opensource-форк Vault, лицензия без BSL) на raft-хранении в PVC; бутстреппер auto-init/unseal (ключи в `infra/.vault-unseal-keys.json`, gitignored). Апгрейд-путь: raft из 3 узлов / KMS auto-unseal — при выходе за ноутбук.
- [x] **Post-rollout metric regression monitoring** (mandatory, M6): after every rollout — automatic comparison of key metrics (error rate, latency p95/p99, saturation) against the pre-rollout baseline; degradation threshold → alert + automatic rollback (Argo Rollouts AnalysisTemplates + k6 synthetic traffic); no silent degradation: a rollout is not considered successful without a positive metrics-analysis verdict. **Done in M6 (v0.7.0):** canary-vs-stable + post-rollout analysis, automatic rollback, postmortem draft.
- [ ] agent_api GUI panel (currently CLI/API) — after M3 stabilizes. Includes the 📁 Projects tab + onboarding wizard (see spec §13).
- [ ] Vault: migrate from dev-mode to raft/file backend — when real secrets appear (M3).
- [ ] Taint tracking for untrusted content — post-v0 (trigger: content sources beyond the tracker).

## M4c follow-ups (deferred minors from the final review, 2026-11-15)

- [ ] Control-plane `complete/fail` raising → tracker never updated and nothing audited: post a best-effort note / audit `tracker_skipped`.
- [ ] `set_task_status` does PATCH then comment (non-atomic): comment first.
- [ ] in-progress/blocked reopen an issue a human closed: skip the PATCH when the issue is closed and status != done.
- [ ] Raw failure text (exception messages) is posted to issue comments: truncate, or post reason codes.
- [ ] Secrets on command lines (`--from-literal`, `curl -H "Authorization…"`, python argv) in `section_mcp`, `section_litellm` and `m4c_flow`: use stdin, env or header files.
- [ ] Collapsed line continuations (long space runs) in `bootstrap.sh` `section_mcp` and `m4c_flow.sh`: cosmetic.
- [ ] Poller checkpoint written after the loop, in `$TMP`; no server-side uniqueness on `task_ref` (fine for acceptance, not for a real poller).
- [ ] `list_issues` has a fixed `limit=50` and no pagination.
- [ ] MCP Dockerfile: unpinned pip install (not from `uv.lock`), no `.dockerignore`.
- [ ] Worker `attempts >= 3` is checked after the claim increment, so only 2 real executions happen (pre-existing).
- [ ] `llm.complete` bypasses token budgets (M5); the git MCP server is built but not wired (M5 scoped tokens).
- [ ] MCP servers have no auth token; sandboxes use the LiteLLM master key: add an MCP auth token and a scoped LiteLLM virtual key per run/role.
- [x] `org-bootstrap check` marks a tool `[ok]` even when `--version` fails: check the exit code. Done by an agent through the self-development corridor (Gitea PR #3 → GitHub PR #29, 2026-11-15).
- [ ] LLM: set `OPENROUTER_API_KEY` in `.env`, so the fallback works when the z.ai Coding Plan limit is hit. z.ai ToS lists supported tools only (LiteLLM is not listed): if the plan gets restricted, route z.ai only through the OpenCode provider.

## M5 follow-ups (2026-11-15)

- [ ] Live forced-red fix-loop scenario in `m5_flow` (deterministic CI failure injected before review). The fix loop is unit-proven (gate driver + real-git fix port) but has not yet happened live. (2026-11-16: a real fix round happened live in m5 run 22 — it exposed fix evidence overwriting the vertex manifest, fixed: `evidence_suffix=fix-N`.)
- [ ] Agent-authored replies to review threads: v0 posts "addressed in fix round N" per blocking finding; the next review round re-judges.
- [ ] Read-scoped Gitea tokens per project for the `ci` / `task-reader` MCP servers and the reviewer (v0 reuses the admin token).
- [ ] Webhook-driven gate controller in agent_api (M8, swarm scale): the corridor now runs inside the run-scoped worker, which holds the lease while waiting for CI/HITL.
- [ ] More check profiles (Java, TS/React, LLM-assistant evals) as data under `infra/templates/check-profiles/`.
- [ ] `m5_flow`: the org-variable POST prints a harmless 409 on re-runs (it falls back to PUT) — silence it.
- [ ] HITL approve UX: a CLI command (`org-bootstrap hitl approve <id>`) until the M10 panel.
- [ ] Gate wait ignores lease loss: stop the corridor when the heartbeat reports the lease lost (today a worker could still merge).
- [x] A HITL-rejected merge should be terminal for the vertex (today agent_api requeues it and the agent retries, asking the human again). Done in M6: `fail(..., terminal=True)` → vertex `failed`.
- [x] `bootstrap.gitea.GiteaClient.put_file` ignores HTTP errors (pre-existing): raise, so seeding onto a protected `main` is visible. Done in self-dev level 2 (also skips identical content).

## Self-development level 2 follow-ups (2026-11-15)

- [ ] Platform CI duplication: `.gitea/workflows/ci.yml` (platform profile) and `.github/workflows/ci.yml` run the same gates in two files — a test that keeps their `checks` steps equivalent.
- [ ] `selfdev_flow.sh` holds the worker (and its lease) in the foreground while the human decides HITL — fine for one task; the webhook-driven gate controller (M8) removes the wait.
- [ ] Task text reaches the agent as the 120-char vertex `name` + the issue via the task-reader MCP: give agent_api a free-text task body field.
- [ ] HITL approve UX for the platform repo: `org-bootstrap hitl approve <id>` (shared with the M5 item).
- [ ] Automate task intake for the platform repo (STATE/TODO → tracker issues) — today a human files them.

## M6 follow-ups (2026-11-16)

- [ ] Deploy verdict lists every AnalysisRun of the pod-template hash (including runs of an earlier aborted attempt): filter by rollout revision.
- [ ] No image registry: the deployer builds on the host engine shared with k3s (Rancher) or imports into k3d; a remote cluster needs a registry push (Gitea packages).
- [ ] The deployer holds the worker lease during the rollout (~5 min): move rollout watching to the webhook gate controller (M8).
- [ ] Postmortem drafts stay in the evidence: write them back to episodic project memory (M7).
- [ ] Operator changes to `deploy/**` are manual Gitea PRs: add `org-bootstrap deploy-config` (or a HITL-gated agent role).
- [ ] Canary analysis thresholds are hard-coded in the template: per-service SLO file (spec §7.2.6).

## M7 follow-ups (2026-11-16)

- [ ] Embeddings (pgvector) for retrieval — needs an embedding model through LiteLLM; the z.ai Coding Plan serves chat only (ADR-0003 revisit trigger: retrieval quality on the dogfood corpus).
- [ ] Code indexing (context_engine) and spec self-evals (spec §8 M7) — separate subsystems.
- [ ] Automatic lesson promotion after ~10 dogfood tasks with eval on historical tasks (ADR-0003).
- [ ] Read-only `memory` MCP server for on-demand search by agents (today: slice in the prompt only).
- [ ] Memory outages are logged, not audited (`memory_skipped`): the fail-open client cannot tell an outage from an empty slice.
- [ ] Episode TTL / consolidation job (drift and poisoning guard).
- [ ] A same-image deploy with a config change (fault env) gets a postmortem that only names the image (`rolled back to <same image>`): include the env/config diff.

## M8 follow-ups (2026-11-16)

- [ ] Tracker status for multi-vertex runs: every vertex mirrors into the same issue (a finished vertex closes it while others still run) — aggregate per run.
- [ ] Baseline invalidation and coordinator reset (spec §7.3): vertices are not re-planned when main moves under them.
- [ ] Specialist consult roles (architecture/security/tests) beyond the bot reviewer; judge as an LLM role for non-test criteria.
- [ ] Working-memory scratchpad for the coordinator (today: deps' results are the handoff).
- [ ] Workers on several hosts (today: one host, N processes); NodePort pool 30100–30110 caps concurrent sandboxes at 11.
- [x] Scrape the control-plane `/metrics` — **done in M11** (ServiceMonitor `agent-api`).

## M9 follow-ups (2026-11-16)

- [ ] Java check profile + Spring/plain-Java service template (compile, Checkstyle, SpotBugs, JUnit via a JDK container).
- [ ] React/frontend profile (eslint, tsc, vitest; browser e2e later).
- [ ] LLM-assistant profile: eval suites as first-class tests, prompt versions in the run manifest (spec §7.2.4).
- [ ] Per-kind templates and tutorials `docs/tutorials/onboard-<kind>.md` (spec §13.1.5).
- [ ] Canary deploy for non-Python services: the M6 deploy templates are service-agnostic but only isv-product is wired.

## M11 follow-ups (2026-11-16)

- [ ] **Stand LAN exposure:** Rancher Desktop forwards every NodePort on `0.0.0.0` and the Windows firewall allows `host-switch.exe` inbound on Private/Domain networks. The control plane now requires a token, but the task-source MCP (:30085) and Prometheus (:30090) have no auth. Owner decision: restrict the two "Rancher Desktop Networking … Exception" rules, or add auth to those services.
- [ ] Per-identity service tokens (today one shared `secret/platform/api` token for the daemon, workers and poller); rotation.
- [ ] Retry of a failed task: today one issue = one run (dedup by `task_ref`, any status); a `swarm-retry` label or a panel action.
- [ ] Graceful worker shutdown on daemon restart (SIGTERM → finish/abandon the session, tear the sandbox down); today leases expire and the orphan sweep reclaims sandboxes.
- [ ] Browser e2e in CI (Playwright against a local agent_api with SQLite).
- [ ] Baseline invalidation and coordinator reset (spec §7.3): re-plan vertices when `main` moves under them.
- [ ] Remove the stale Gitea branch `platform-live` once the pinned commit is on `main` (`platform.sh up` from main does not need it).

## M10 follow-ups (2026-11-16)

- [ ] OIDC/SSO for the panel; server-side user profiles (language preference).
- [ ] Pipeline kanban (task → spec → code → gates → review → canary → prod) and Settings tab.
- [x] Browser e2e tests of the panel — **done in M11** (Playwright, `infra/tests/e2e`); running it in CI: open.
- [x] Cron scheduling of automations — **done in M11** (the daemon's poller evaluates `trigger: cron`).
- [ ] The other starter automations (dep-updates, log-analysis, feedback-ingest) and the 26-item catalogue as workflow functions.
- [ ] Charts in Metrics (trusted change, time-to-accept, DORA, verification debt).
- [x] Deploy agent_api + the panel in-cluster — **done in M11** (Argo CD app `platform-control`, NodePort 30400).
