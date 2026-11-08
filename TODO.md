# TODO

## Tracker task-status feedback loop (mandatory)

- [ ] Close the feedback loop: the platform must change the tracker task status as agents work.
  - Task picked up by an agent → status **In Progress** (automatically, via the MCP task-source).
  - Key pipeline milestones → task comments: spec ready (link), PR created (link), acceptance passed, rollout done.
  - Completion → **Done** + links to PR and evidence pack. Failure/block → **Blocked** with a reason.
  - Rule: an agent never leaves a task in its original status — the feedback loop is closed in both directions (tracker → platform → tracker).
  - Implementation: callbacks via the MCP task-source server (OpenProject/Jira adapter, spec §7.1), milestone M3/M5.

## Other

- [ ] **OpenProject password/basic-auth login broken** (v17-slim): rails `check_password?` = true, но web login 422 и API basic 401. Расследовать (session store/memcached? Enterprise-флаги?) или перейти на OIDC-плагин; до тех пор трекер v0 — Gitea issues (TaskSource poller → M4 MCP task-source).
- [x] ~~Vault dev-mode → постоянный backend~~ ✅ 2026-11-08: **OpenBao** (opensource-форк Vault, лицензия без BSL) на raft-хранении в PVC; бутстреппер auto-init/unseal (ключи в `infra/.vault-unseal-keys.json`, gitignored). Апгрейд-путь: raft из 3 узлов / KMS auto-unseal — при выходе за ноутбук.
- [ ] **Post-rollout metric regression monitoring** (mandatory, M6): after every rollout — automatic comparison of key metrics (error rate, latency p95/p99, saturation) against the pre-rollout baseline; degradation threshold → alert + automatic rollback (Argo Rollouts AnalysisTemplates + k6 synthetic traffic); no silent degradation: a rollout is not considered successful without a positive metrics-analysis verdict.
- [ ] agent_api GUI panel (currently CLI/API) — after M3 stabilizes. Includes the 📁 Projects tab + onboarding wizard (see spec §13).
- [ ] Vault: migrate from dev-mode to raft/file backend — when real secrets appear (M3).
- [ ] Taint tracking for untrusted content — post-v0 (trigger: content sources beyond the tracker).
