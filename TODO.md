# TODO

## Tracker task-status feedback loop (mandatory)

- [ ] Close the feedback loop: the platform must change the tracker task status as agents work.
  - Task picked up by an agent → status **In Progress** (automatically, via the MCP task-source).
  - Key pipeline milestones → task comments: spec ready (link), PR created (link), acceptance passed, rollout done.
  - Completion → **Done** + links to PR and evidence pack. Failure/block → **Blocked** with a reason.
  - Rule: an agent never leaves a task in its original status — the feedback loop is closed in both directions (tracker → platform → tracker).
  - Implementation: callbacks via the MCP task-source server (OpenProject/Jira adapter, spec §7.1), milestone M3/M5.

## Other

- [ ] **Post-rollout metric regression monitoring** (mandatory, M6): after every rollout — automatic comparison of key metrics (error rate, latency p95/p99, saturation) against the pre-rollout baseline; degradation threshold → alert + automatic rollback (Argo Rollouts AnalysisTemplates + k6 synthetic traffic); no silent degradation: a rollout is not considered successful without a positive metrics-analysis verdict.
- [ ] agent_api GUI panel (currently CLI/API) — after M3 stabilizes.
- [ ] Vault: migrate from dev-mode to raft/file backend — when real secrets appear (M3).
- [ ] Taint tracking for untrusted content — post-v0 (trigger: content sources beyond the tracker).
