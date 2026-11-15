# Self-development level 2: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (owner pre-authorized inline execution). Steps use checkbox (`- [ ]`) syntax.

**Goal:** this repo lives as `swarm-lab/ai-based-isv` in the lab Gitea. Platform tasks go through the M5 corridor under a non-admin agent identity and are promoted to GitHub via PR.

**Architecture:** GitHub stays canonical. `platform-sync.sh` fast-forwards Gitea from GitHub and promotes Gitea `main` back through a GitHub PR. Agents get a dedicated `swarm-agent` user with write access on the `swarm-lab` org; the admin token leaves the sandboxes.

**Tech Stack:** Python 3.12 (uv workspace, httpx, pydantic-settings), bash in the toolbox, Gitea API, gh CLI.

**Spec:** `docs/superpowers/specs/2026-11-15-selfdev-level2-design.md`

## Global Constraints

- Gitea platform repo: `swarm-lab/ai-based-isv`. GitHub canonical: `kryadov/ai-based-isv`.
- CI contexts: exactly `ci / checks (pull_request)` and `ci / security (pull_request)`.
- Agent identity: user `swarm-agent`, OpenBao `secret/gitea/agent` (fields `username`, `password`, `token`). Team `agents` in org `swarm-lab` with permission `write` and `includes_all_repositories: true`.
- No fallback to `secret/gitea/admin` anywhere in the agent path (fail closed).
- Platform repo: never auto-merge. Push whitelist on `main` = `["swarm-admin"]` only.
- English in code and docs; `*.ru.md` for Russian. Never `git add -A`.
- LLM: z.ai only until its limit (AGENTS.md §10).

## Review Focus

1. **Bot authors and reviews its own PR.** A blocking review must still block and trigger a fix round, with no Gitea 422. Pinned in Task 1.
2. **`secret/gitea/agent` missing** (stand not re-bootstrapped). The worker must fail with a clear error, never fall back to admin. Pinned in Task 2.
3. **`pull` when Gitea `main` holds unpromoted commits.** It must refuse and push nothing. Pinned in Task 6.
4. **Re-running onboarding on the platform repo.** It must create no new commits on `main`, because files are identical. Pinned in Task 4.
5. **A worker serving a target repo** must keep `platform_repo=False` and docs auto-merge working. Pinned in Task 5.

---

### Task 1: Reviews are always posted as COMMENT

**Files:** Modify `packages/agent_runtime/src/agent_runtime/gates.py` (~line 170). Test: `packages/agent_runtime/tests/test_gates.py`.

- [ ] Update `test_blocking_review_triggers_fix_and_replies_posted`: `assert pr.reviews[0][0] == "COMMENT"` and `assert pr.reviews[0][1].startswith("**Changes requested**")`.
- [ ] Run it: FAIL (event is `REQUEST_CHANGES`).
- [ ] In `gates.py`: `event="COMMENT"` always. Prefix the body with `**Changes requested**\n\n` when `result.blocking`. Add a comment explaining that Gitea forbids REQUEST_CHANGES on one's own PR.
- [ ] Run `test_gates.py`: PASS. Commit `fix(gates): post bot reviews as COMMENT (bot is PR author)`.

### Task 2: Non-admin agent identity

**Files:**
- Modify `infra/scripts/bootstrap.sh`: new `section_agent_user`, case `agent-user`, added to `all` right after `gitea`.
- Modify `packages/agent_runtime/src/agent_runtime/vault_broker.py` and its test (`packages/agent_runtime/tests/test_vault_broker.py`).
- Test: `apps/bootstrap/tests/test_agent_user_section.py`.

**Interfaces:**
- Produces `read_gitea_token(kubectl_runner, *, root_token, namespace="platform", pod_label=..., path: str = AGENT_TOKEN_PATH) -> str`, with `AGENT_TOKEN_PATH = "secret/gitea/agent"`.

Steps:

- [ ] **Test the broker reads the agent path.** `test_reads_agent_token_path_not_admin`: the fake runner captures stdin and argv. Assert that the remote script contains `secret/gitea/agent` and does not contain `secret/gitea/admin`.
- [ ] **Test the missing-secret error.** `test_missing_agent_secret_fails_closed`: the runner returns rc=2. Expect `VaultBrokerError` whose message contains `secret/gitea/agent` and `bootstrap.sh agent-user`.
- [ ] **Test the bootstrap section text.** In `test_agent_user_section.py`, `bootstrap.sh` must contain:
  - `section_agent_user`;
  - `secret/gitea/agent`;
  - `/api/v1/admin/users`;
  - `includes_all_repositories`;
  - `"permission":"write"`;
  - `agent-user) section_agent_user`.

  In the `all` list, `agent-user` must come after `gitea`.
- [ ] Run all three tests: FAIL.
- [ ] **Implement the broker change.** Thread `path` into the remote script, update the docstring to drop the v0 admin exception, and make the error text name the section.
- [ ] **Implement `section_agent_user`.** All steps are idempotent:
  1. Password from OpenBao, or generate one.
  2. `POST /api/v1/admin/users` with `{username: swarm-agent, email: swarm-agent@lab.local, password, must_change_password: false}`; a 422 means the user exists.
  3. Validate the existing token via `GET /api/v1/user`. Otherwise create one with basic auth `swarm-agent:$pw` via `POST /api/v1/users/swarm-agent/tokens`, scopes `["write:repository","write:issue","read:user","read:organization"]`.
  4. Ensure the org `swarm-lab`.
  5. Find or create team `agents` via `POST /api/v1/orgs/swarm-lab/teams` with `{name: agents, permission: write, includes_all_repositories: true, units: [repo.code, repo.pulls, repo.issues, repo.actions]}`.
  6. Add the member: `PUT /api/v1/teams/{id}/members/swarm-agent`.
  7. `vcli kv put secret/gitea/agent username=swarm-agent password=… token=…`.

  JSON is parsed with python3 inside the toolbox, as in `ensure_admin_token`.
- [ ] **Live check.** Run `./infra/scripts/toolbox.sh bash infra/scripts/bootstrap.sh agent-user` twice; both runs must exit 0. Then `curl` `/api/v1/user` with the agent token must return `"login":"swarm-agent"` and `"is_admin":false`.
- [ ] Docs: INSTALL (+ru) lists the section and explains why. Commit `feat(security): agents use non-admin swarm-agent Gitea identity`.

### Task 3: Platform check profile

**Files:**
- Create `infra/templates/check-profiles/platform/.gitea/workflows/ci.yml` and `infra/templates/check-profiles/platform/.platform/merge-policy.yaml`.
- Create root copies `.gitea/workflows/ci.yml` and `.platform/merge-policy.yaml`. The latter exists; replace it with the template content.
- Test: `apps/bootstrap/tests/test_profiles.py`.

**Template contents:**
- `ci.yml` is a copy of the python profile with one change: the `checks` job runs
  - `uv sync --all-packages --locked`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run mypy packages apps`
  - `uv run pytest -q`

  The workspace pins its own tool versions, so there are no `--with` pins. The `security` job is unchanged.
- `merge-policy.yaml`: `auto_merge: {enabled: false, paths: []}` with a comment pointing to §13.7.

Steps:

- [ ] Write the tests:
  - `test_platform_profile_runs_workspace_gates`: the checks steps contain `mypy packages apps` and `--all-packages --locked`.
  - `test_platform_profile_reports_required_contexts`: workflow `name: ci`, jobs `checks` and `security`.
  - `test_repo_root_carries_platform_profile`: for each `profile_files("platform")` item, the repo-root file equals it byte for byte.
  - `test_platform_policy_never_auto_merges`: `load_policy(text, platform_repo=False).auto_merge_enabled is False`.
- [ ] Run them: FAIL.
- [ ] Create the files.
- [ ] Verify locally in the toolbox:
  - `uvx --with "pydantic<2.12" "semgrep==1.140.0" scan --config p/ci --error --metrics off .`
  - gitleaks `detect --no-git --redact --source .` via the same checksum-verified download into the toolbox temp dir

  Fix real findings. Silence a false positive only with an inline justification.
- [ ] Commit `feat(selfdev): platform check profile (+ repo-root copy)`.

### Task 4: Idempotent, fail-loud profile seeding and a push whitelist

**Files:** Modify `apps/bootstrap/src/bootstrap/gitea.py` (`put_file`, `apply_check_profile`) and `apps/bootstrap/src/bootstrap/cli.py` (`onboard --push-whitelist`). Test: `apps/bootstrap/tests/test_gitea_profile.py`.

**Interfaces:** `apply_check_profile(repo, *, stack, git_url, push_whitelist: Sequence[str] = ()) -> None`.

- [ ] Write the tests with the existing fake transport:
  - `test_identical_files_are_not_rewritten`: GET returns base64 of the identical content, so no PUT or POST is made for that path.
  - `test_put_file_raises_on_http_error`: PUT returns 403, so `RuntimeError` is raised and the message contains the path.
  - `test_push_whitelist_in_protection`: the protection payload has `enable_push: True`, `enable_push_whitelist: True` and `push_whitelist_usernames: ["swarm-admin"]`.
  - `test_default_protection_has_no_push`: without a whitelist, the payload has no `enable_push` key.
- [ ] Run them: FAIL.
- [ ] Implement:
  - `put_file` decodes the existing `content`, returns early when it is equal, and raises on a code ≥ 400.
  - `onboard` gets `push_whitelist: list[str] = typer.Option([], "--push-whitelist")`.
- [ ] PASS. Update TODO (+ru) to strike "put_file ignores errors". Commit.

### Task 5: Per-repo platform detection in the worker

**Files:** Modify `packages/agent_runtime/src/agent_runtime/settings.py` and `worker_gates.py` (`default_gate_runner`). Test: `packages/agent_runtime/tests/test_worker_gates.py` (create the file if absent).

**Interfaces:**
- `WorkerSettings.platform_repos: list[str] = ["swarm-lab/ai-based-isv"]`.
- `is_platform_repo(settings: WorkerSettings, owner: str, repo: str) -> bool` in `worker_gates.py`, true if `settings.platform_repo` or `f"{owner}/{repo}" in settings.platform_repos`.

Steps:

- [ ] Write `test_platform_repo_detected_by_slug`, `test_target_repo_is_not_platform` and `test_explicit_flag_still_wins`.
- [ ] Run them: FAIL.
- [ ] Implement; `GateConfig(platform_repo=is_platform_repo(settings, owner, repo))`.
- [ ] PASS. Commit.

### Task 6: `infra/scripts/platform-sync.sh`, subcommands `init | pull | promote`

**Files:** Create `infra/scripts/platform-sync.sh`. Test: `apps/bootstrap/tests/test_platform_sync.py`, which runs the script against local bare repos and needs only git and bash.

**Inputs:** env overrides for tests:
- `SYNC_GITHUB_URL`, default `https://github.com/kryadov/ai-based-isv.git`;
- `SYNC_GITEA_URL`, default `http://localhost:30080/swarm-lab/ai-based-isv.git`;
- `SYNC_GITEA_TOKEN`, default read from OpenBao `secret/gitea/admin`.

Git auth uses `GIT_CONFIG_*` env (an `http.extraHeader` of the form `Authorization: token …`), never the URL or argv.

- [ ] Write the tests. Each one makes `gh.git` and `gt.git` bare repos with a shared base commit:
  - `test_pull_fast_forwards_gitea`: GitHub is ahead, so after `pull` the Gitea main SHA equals the GitHub one.
  - `test_pull_refuses_unpromoted_gitea_commits`: Gitea is ahead or diverged, so the exit code is non-zero, stderr contains `unpromoted`, and Gitea main is unchanged.
  - `test_promote_pushes_selfdev_branch`: with `SYNC_NO_PR=1`, GitHub has a branch `selfdev/<short-sha of gitea main>` and its main is unchanged.
  - `test_promote_noop_when_in_sync`: exit 0, prints `nothing to promote`, no branch.
- [ ] Run them: FAIL.
- [ ] Implement:
  - `pull`: fetch both. If `merge-base --is-ancestor gitea/main github/main` holds, push `github/main:refs/heads/main` to Gitea; otherwise exit 3 with `unpromoted`.
  - `promote`: if Gitea main is an ancestor of GitHub main, print `nothing to promote`. Otherwise push to `selfdev/<sha7>` and, unless `SYNC_NO_PR`, run `gh pr create --base main --head selfdev/<sha7>` with a title and a body built from `git log --merges --format='- %s' github/main..gitea/main`.
  - `init`: create the org repo (`auto_init: false`) if absent, push GitHub main, then call `org-bootstrap onboard … --stack platform --push-whitelist swarm-admin`.
- [ ] PASS. Commit.

### Task 7: Live level-2 acceptance

**Files:** Create `infra/tests/selfdev_flow.sh`. Modify STATE, ARCH, INSTALL, TODO and AGENTS (+ru).

1. [ ] Re-bootstrap the `agent-user` section, then re-run `m5_flow.sh` to prove the corridor works under `swarm-agent`. Expect PASS.
2. [ ] `selfdev_flow.sh`:
   1. `platform-sync.sh init` (idempotent) and `pull`;
   2. create the task through the same control-plane path `m5_flow.sh` uses, with `repo_url=http://gitea-http.platform.svc.cluster.local:3000/swarm-lab/ai-based-isv.git`; the goal is the TODO item "HITL reject is not terminal — make a rejected merge terminal (tracker task Failed, no retry)", spelled out with the test to add;
   3. start the worker with `AGENT_RUNTIME_GATES_ENABLED=1`;
   4. wait for the HITL request and print the PR URL and the HITL id;
   5. exit 0 with "awaiting HITL", without auto-approving.
3. [ ] Read the PR diff, CI and review. Approve the HITL as the owner-pre-authorized operator. Watch for the merge and the tracker Done.
4. [ ] `platform-sync.sh promote` → GitHub PR → GitHub CI green → `gh pr merge --merge` → `platform-sync.sh pull` → `release.sh v0.6.0`.
5. [ ] Update the docs:
   - STATE: level 2 achieved, with the evidence.
   - ARCH (+ru): self-dev loop diagram.
   - INSTALL (+ru): `platform-sync.sh` usage.
   - AGENTS (+ru): a short §11 "Self-development workflow".
   - TODO (+ru).

   Commit and push.

## Self-review notes

- Every spec section has a task:
  - §1 → Task 3; §2 → Task 4; §3 → Tasks 1–2; §3b → Task 4; §4 → Task 6; §5 → Task 5; §6–7 → Task 7.
- Type names are consistent: `is_platform_repo`, `AGENT_TOKEN_PATH`, `push_whitelist`.
