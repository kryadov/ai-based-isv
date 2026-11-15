# M10 Panel + Automations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (owner pre-authorized inline execution).

**Goal:** humans operate the platform from a logged-in panel (Inbox decisions, Swarm, Projects, Memory, Metrics, Automations, EN/RU), and the vuln-scan automation turns findings into tracker issues only after approval.

**Spec:** `docs/superpowers/specs/2026-11-16-m10-ui-automations-design.md`

## Global Constraints

- **UI:** `/ui`, served by agent_api, no build step; i18n `en.json` and `ru.json` with identical keys.
- **Sessions:** `AGENT_API_UI_USERS` (`name:sha256hex:role`) and `AGENT_API_SESSION_SECRET`. Cookie `swarm_session`, HMAC-SHA256, 12 h, `HttpOnly`, `SameSite=Strict`. No users → 503.
- **Automations:** definitions in `infra/automations/*.yaml`. Run statuses: `requested`, `running`, `awaiting_approval`, `done`, `failed`. HITL kind `automation`. Issue title prefix `[vuln-scan]`.

## Review Focus

1. **Tampered or expired cookie.** It must be refused. Test.
2. **No users configured.** 503, never open. Test.
3. **UI decision on an already-decided HITL.** 409, no double decision. Test.
4. **vuln-scan when a tool is missing or crashes.** The run is `failed` with the reason, and no card is created. Test.
5. **A rejected automation card.** No issues are created. Test.

### Task 1: Auth + UI shell (agent_api)

`ui_auth.py` provides `hash_password`, `parse_users`, `sign`, `verify`. Routes:
- `GET /ui` (login page or app shell);
- `POST /ui/login`, `POST /ui/logout`;
- `GET /ui/static/*`;
- `GET /ui/api/me`;
- `POST /ui/api/hitl/{id}/decision`.

Steps:
- [ ] Write the tests:
  - login ok/bad;
  - a tampered or expired cookie is refused;
  - no users → 503;
  - the shell contains every tab id;
  - the en/ru keys are equal;
  - a decision records `decided_by=user`;
  - a second decision → 409.
- [ ] FAIL → implement (static `index.html`, `app.js`, `styles.css`, `i18n/*.json`) → PASS. Commit.

### Task 2: Automation runs in the control plane

The table `automation_runs` and the endpoints:
- `POST /automations/runs` (request or record a run);
- `PATCH /automations/runs/{id}`;
- `GET /automations/runs?name=&status=`;
- `GET /automations` (definitions from `AGENT_API_AUTOMATIONS_DIR`);
- UI: `POST /ui/api/automations/{name}/run` (requested by user).

Steps:
- [ ] Write the tests for create, list, filter, patch status/report/hitl_id and the definitions listing.
- [ ] FAIL → implement → PASS. Commit.

### Task 3: Automation engine + vuln-scan (`agent_runtime/automations.py`)

It provides `load_definition`, `Finding`, `parse_semgrep`, `parse_gitleaks`, `run_vuln_scan`, `await_decision_and_act`, a CLI `run|poll`, and `issue_body(repo, findings)`.

Steps:
- [ ] Write the tests with fake tool outputs and a fake Gitea/API:
  - findings parsed from real-shaped JSON;
  - a card is created with the summary;
  - approve → one issue per repo with findings;
  - reject → none;
  - a tool crash → run `failed`, no card;
  - zero findings → `done` without a card.
- [ ] FAIL → implement → PASS.
- [ ] Add `infra/automations/vuln-scan.yaml`.
- [ ] Commit.

### Task 4: Acceptance, docs, release v1.0.0

- [ ] `infra/tests/m10_flow.sh`:
  1. Create `swarm-lab/vuln-demo` with a semgrep-detectable pattern.
  2. Start agent_api with one UI user.
  3. Check login/refusal and the shell over HTTP.
  4. A pending merge HITL is decided through `/ui/api` as the user.
  5. Request vuln-scan from the UI API, then run `automations poll` once: a card appears; approve it through the UI API; the engine creates the issue.
  6. Assert that the issue exists in vuln-demo.
- [ ] Run it live: PASS.
- [ ] Update the docs: TUTORIAL §11 "The panel" (+ru), README, ARCH, STATE (M10 closed, v1.0.0), TODO.
- [ ] PR → CI → merge → release **v1.0.0** → platform-sync pull.
