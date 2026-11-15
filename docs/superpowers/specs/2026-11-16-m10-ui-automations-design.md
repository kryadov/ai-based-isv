# M10: organization panel + Automations (v0)

Spec §7.1, §8 M10 and the addendum `2026-11-08-ui-and-automations-addendum.md` (the owner approved the tabs, cards and catalogue there). The owner said to continue to the end without stopping. Every "Ruling" is my call, open to correction.

## Intent

The platform is operated today through scripts and `curl`. M10 gives humans **one place** to work:
- **Inbox** of decisions: merge, deploy, escalation and automation cards, with approve/reject;
- **Swarm** (runs and their vertex graphs), **Projects**, **Memory** and **Metrics**;
- **Automations**: recurring organizational workflows on the same engine. A run produces a report and an Inbox card; actions such as creating fix tasks happen only after a human approves.

**Acceptance (`m10_flow.sh`):**
1. A human logs into the panel; an unauthenticated request is refused.
2. The shell renders all tabs, in EN and RU.
3. An Inbox card for a pending merge is decided from the panel, recorded under the human's name.
4. The **vuln-scan** automation runs on a demo repo with a known vulnerable pattern. Its findings produce an Inbox card, and approving it creates tracker issues in that repo.

## Rulings

- **Frontend:** dependency-free ES modules and CSS, served by `agent_api` at `/ui` with no build step. The addendum left the technology to the builder. React adds a toolchain for no v0 gain; the API-first design keeps a later React rewrite cheap. Browser e2e tests: TODO.
- **Auth (humans only):** local users from `AGENT_API_UI_USERS` (`name:sha256(password):role`, comma-separated). The session is a signed cookie: HMAC-SHA256 with `AGENT_API_SESSION_SECRET`, 12 h, `HttpOnly` and `SameSite=Strict`.
  - Without configured users the panel is **disabled**, a 503 that explains why. It never runs open.
  - Every UI action goes through `/ui/api/*` behind the session and records the user as `decided_by`.
  - The existing JSON API stays as it is, for service identities on the host. OIDC: TODO.
- **Language:** English base, RU toggle in the top bar; strings live in `ui/i18n/{en,ru}.json` with identical key sets (test). The choice is stored per browser (localStorage). Server-side profiles: TODO.
- **Automations as code:** `infra/automations/<name>.yaml` with `{name, description, trigger: manual|cron, schedule?, scope: [owner/repo], role, budget_minutes, workflow}`.
  - The control plane stores **runs**: table `automation_runs` with `{id, name, status requested|running|awaiting_approval|done|failed, report, hitl_id, created_issues}`.
  - The host-side engine `python -m agent_runtime.automations` has `run <name>` (manual or cron) and `poll`, which executes runs requested from the panel.
  - Cron via a k8s CronJob is TODO; agent_api still runs on the host.
- **v0 workflow implemented: `vuln-scan`.** For each repo in scope it clones the repo, runs `semgrep --config p/ci --json` and `gitleaks detect --report-format json`, and collects findings (`repo`, `tool`, `rule`, `path`, `line`, `severity`). Then:
  - the report is stored;
  - a HITL card of kind `automation` is created, with a summary and the top findings;
  - on **approve**, one tracker issue per repo is created, listing its findings (label/title prefix `[vuln-scan]`);
  - on **reject**, nothing happens.

  The other 3 starter automations and the 26-item catalogue are TODO entries; they plug in as new workflow functions.
- **Tabs in v0:** Inbox, Swarm, Projects, Automations, Memory and Metrics are functional. Pipeline (kanban) and Settings are placeholders that say "planned".
- **Org health strip** under Inbox: pending decisions, runs done/failed, vertices running, memory episodes. These are proxies for the addendum's 5 metrics; the full DORA/verification-debt charts are TODO.

## Non-goals

- OIDC/SSO.
- React.
- Browser e2e tests.
- Pipeline kanban.
- Cron scheduling in-cluster.
- Automations beyond vuln-scan.
- Charts.

## Anti-goals

- A panel reachable without login when users are configured, or open when they are not.
- An automation creating tasks without a human approval.
- Agents logging into the panel (they are entities on the Swarm/Memory tabs).
