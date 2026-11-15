# Self-development level 2: the platform repo through its own corridor

Spec §13.7 level 2. Owner decision (2026-11-15): do this before M6. The owner is away and asked for continuous progress with critical questions only, so every "Ruling" below is my call, open to correction.

## Intent

Get development "on rails": platform work is filed as a tracker task, an agent implements it in a sandbox, and the M5 corridor carries it to a merge:

**PR → CI → bot review → fix rounds → HITL → merge**

The human-agent pair stops being the only way platform code changes.

**Acceptance (§13.7):** one real platform change from TODO goes end to end:
1. a tracker task;
2. an agent PR in Gitea;
3. green CI, bot review and a HITL merge;
4. promotion to GitHub;
5. a release.

The result is recorded in STATE.md.

## Constraints

- **The canonical repo stays GitHub** (spec §6.1, §14). Gitea `swarm-lab/ai-based-isv` is the working copy agents commit to. Platform releases remain GitHub Releases.
- **No auto-merge on the platform repo.** HITL approves every merge (§13.7). This is already enforced: with `platform_repo=True`, `load_policy` disables auto-merge.
- **No direct pushes to GitHub `main`** (AGENTS.md §6). Promotion goes through a GitHub PR plus GitHub CI. The owner pre-authorized merges and releases after a green quality gate.
- **LLM traffic follows the z.ai-first rule:** OpenRouter only once the z.ai limit is hit.
- **Agents cannot change gate files.** `.gitea/**` and `.platform/**` are already protected in M5.

## Approaches

1. **Bidirectional mirror (Gitea push-mirror to GitHub).** Every Gitea merge lands on GitHub `main` automatically. Rejected: it pushes to GitHub `main` directly, and GitHub CI would run only after the fact.
2. **Gitea as a working copy, with explicit sync (chosen).**
   - `pull` fast-forwards Gitea `main` from GitHub `main`.
   - `promote` pushes Gitea `main` to a GitHub branch and opens a GitHub PR.
   - After GitHub CI is green, the PR is merged with a merge commit, and the next `pull` fast-forwards Gitea onto it.
   - Both CIs gate the change, and nothing touches GitHub `main` directly.
3. **Agents push straight to GitHub PRs, no Gitea.** Rejected: it bypasses the corridor (Gitea Actions profile, HITL inbox, bot review). That corridor is exactly what level 2 is meant to exercise.

## Design

### 1. A platform check profile

Files: `infra/templates/check-profiles/platform/.gitea/workflows/ci.yml` and `.platform/merge-policy.yaml`.

The profile reuses the python profile's structure:
- same SHA-pinned checkout;
- checksum-verified uv and gitleaks;
- the same `security` job;
- identical contexts `ci / checks (pull_request)` and `ci / security (pull_request)`.

The `checks` job runs what the GitHub CI runs:
- `uv sync --all-packages --locked`;
- `ruff check .`;
- `ruff format --check .`;
- `mypy packages apps`;
- `pytest`.

The same two files are committed at the repo root (GitHub ignores `.gitea/`). A test asserts the root copies equal the template.

Ruling: one source of truth checked by a test, rather than generating at onboarding. That way Gitea `main` never diverges from GitHub.

### 2. Idempotent profile seeding

`apply_check_profile` skips any file whose content already matches. This makes onboarding the platform repo a no-op for files. `put_file` now raises on HTTP errors, which closes the "put_file ignores errors" follow-up from TODO.

### 3. Agents stop using the admin token (prerequisite)

Verified: `vault_broker.read_gitea_token` hands the worker and every sandbox the Gitea **admin** token (`secret/gitea/admin`). This was an owner-approved v0 exception, but with it an agent working on the platform could rewrite branch protection or its own gates.

Fix:
- A bootstrap section `agent-user` creates the non-admin user `swarm-agent` and stores its token in OpenBao at `secret/gitea/agent` (fields `username`, `password`, `token`).
- It ensures the org `swarm-lab`, where the flows and the platform repo live, and a team `agents` with `write` on all org repos (`includes_all_repositories: true`) with `swarm-agent` as a member. Repos the flows recreate on every run are therefore covered without per-repo grants.
- The broker reads `secret/gitea/agent` and fails closed if it is missing; there is no fallback to the admin token.
- Flows keep using the admin token for setup and assertions only.

Consequence: the bot is now both PR author and reviewer, and Gitea refuses REQUEST_CHANGES on one's own PR. The gate driver therefore always posts the review as `COMMENT` and marks blocking reviews in the body (`**Changes requested**`). Merge blocking never depended on Gitea's review state.

### 3b. Push rights on the platform repo's `main`

Branch protection on `swarm-lab/ai-based-isv` gets `enable_push_whitelist` with exactly `swarm-admin`, the token `platform-sync.sh` uses to fast-forward from GitHub. Target repos keep "no direct pushes".

Ruling: a separate sync user is unnecessary once §3 removes the admin token from agents. Only the owner and the bootstrap tooling hold it.

### 4. `infra/scripts/platform-sync.sh` (toolbox), subcommands `init | pull | promote`

- **`init`:**
  - creates the empty org repo `swarm-lab/ai-based-isv` (`auto_init: false`);
  - pushes GitHub `main` into it;
  - runs `org-bootstrap onboard --project ai-based-isv --owner swarm-lab --stack platform --kind tool --push-whitelist swarm-admin`. Profile files are already identical, so they are skipped; the run sets the variable, the protection and the registry entry.
- **`pull`:** fast-forward only. It refuses if Gitea `main` has commits not on GitHub, meaning unpromoted work.
- **`promote`:**
  - pushes Gitea `main` to the GitHub branch `selfdev/<short-sha>`;
  - runs `gh pr create` with a body listing the Gitea PRs merged since the last promotion;
  - prints the PR URL.

  Merging the GitHub PR stays a separate, explicit step, `gh pr merge --merge` after green CI. The owner's pre-authorization covers it.

### 5. The worker treats the platform repo by slug

Setting `platform_repos: list[str]` defaults to `["swarm-lab/ai-based-isv"]`. The gate config is computed per task as `platform_repo = f"{owner}/{repo}" in settings.platform_repos`, replacing the global bool. One worker can then serve target projects and the platform without accidentally enabling auto-merge on the platform.

### 6. Tasks

Tasks are filed through the existing control-plane/tracker path used by the flows, with `repo_url` pointing at the in-cluster Gitea `swarm-lab/ai-based-isv`.

Ruling: the first dogfood task is the TODO follow-up "HITL reject is not terminal". It is small and self-contained, has a test-first shape, and touches only `gates.py`/`worker.py` with its tests. Fallback task: a docs-only change.

**HITL approver:** the owner is away and pre-authorized merges after a green quality gate. I approve the HITL request only after reading the diff and seeing CI and review green. The approval is recorded in the evidence and STATE as made under that pre-authorization.

### 7. Acceptance script

`infra/tests/selfdev_flow.sh` runs `pull`, files the dogfood task, runs the worker with gates, waits for the HITL request and prints the PR URL. The script does not auto-approve; the HITL step is manual. After the merge, `promote`, the GitHub CI, a GitHub merge and `release.sh v0.6.0` finish the loop.

Ruling: the version is v0.6.0, the first release whose change was made by the platform itself. M6 then becomes v0.7.0.

## Non-goals

- Level-2 automation of task creation from STATE/TODO.
- A GitHub → Gitea webhook (`pull` is manual or scripted).
- Swarm parallelism (M8).
- Canary (M6).

## Anti-goals

- Any path that lets an agent change its own gates or push `main` (Gitea or GitHub).
- Any divergence that requires force-pushes.

## Left to the builder

- Exact Gitea API calls for users, tokens and push whitelists.
- Where `selfdev_flow.sh` reuses the m5_flow helpers.
- Wording of the promotion PR body.
