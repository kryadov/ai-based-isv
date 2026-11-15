# AI-Native ISV — Engineering Guidelines for AI Agents

> Русская версия: [AGENTS.ru.md](AGENTS.ru.md)

## 0. Session bootstrap (mandatory, before everything else)

1. Read **`STATE.md`** — current project state, verified capabilities, current increment, next step. This file is updated at the end of EVERY increment; update it yourself before declaring "done".
2. Skim **`TODO.md`** — the backlog and mandatory feedback loops.
3. Skim **`ARCH.md`** — how the system works, with diagrams (topology, pipeline, swarm, memory, roles catalog §4a).
4. Process rules — below; full design — in the spec (link below). If STATE.md contradicts reality — verify with facts first (`uv run pytest`, acceptance scripts), then fix STATE.md.

This repository is the platform for agent-driven development (swarm orchestration + organization bootstrap + end-to-end delivery). Full design: `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md`; project lifecycle — §13; UI/Automations — the 2026-11-08 addendum.

## 1. Prime Directive: Superpowers Methodology

Every agent working in this repository MUST follow the Superpowers framework (`.agents/skills/`, `.agents/rules/superpowers.md`):

1. **Skills first**: `brainstorming` — before any new feature/spec/design; `writing-plans` / `executing-plans` — for multi-step tasks; `test-driven-development` — RED-GREEN-REFACTOR mandatory; `systematic-debugging` — for bugs; `verification-before-completion` — before any "done".
2. **Quality over speed**: no speculative/untested code; bite-sized tasks; never disable linters/type-checkers/tests to look green.

## 2. Technology is a decision, not an axiom

The platform tech stack is recorded in ADRs and revised via ADRs, not by editing this file:

- `docs/adr/0001-platform-stack.md` — Python ≥3.12, uv-workspace, pydantic v2, mypy --strict, ruff, pytest, FastAPI, Postgres+pgvector, DBOS, FastMCP.
- `docs/adr/0002-executor-opencode.md` — Executor: OpenCode harness in k3s sandbox; PydanticAI for light reasoning roles.
- `docs/adr/0003-memory.md` — project and organization memory.

## 3. PLATFORM code standards (this repository)

- Python `>=3.12`, src-layout, hatchling; package versions — semver.
- Typing: `mypy --strict`, zero suppressions without an ADR-level justification.
- Style: `ruff` (check + format).
- Tests: pytest; TDD mandatory for any code.

## 4. TARGET-project standards (polyglot)

Target projects (Java/Python/TS/React, assistants, tools) do NOT follow section 3. Their standards arrive via the per-stack check profile, which the bootstrapper bakes into the repo template (spec §7.2.4). For LLM projects/assistants: eval suites are first-class tests, prompt versions live in the run manifest. An existing tracker is connected via the `TaskSource` adapter (spec §6.1, §9).

## 5. Repository structure

```
├── .agents/                 # Superpowers skills and rules
├── .github/workflows/       # platform CI (GitHub — canonical repo, spec §6.1)
├── docs/
│   ├── adr/                 # architecture decisions (technology lives there)
│   ├── superpowers/         # specs/, plans/, research/
│   └── architecture/        # diagrams and domain models
├── packages/
│   ├── isv_core/            # domain models and event contracts
│   ├── agent_runtime/       # Executor (OpenCode), k3s sandboxes, session logs
│   ├── context_engine/      # code indexing, project and org memory
│   ├── mcp_servers/         # MCP: task-source, git, ci, deploy/observability, memory, llm
│   └── evals/               # spec self-evals, agent benchmarks
├── apps/
│   ├── bootstrap/           # organization bootstrapper (questionnaire, L0, project seeding)
│   ├── agent_api/           # control plane: queue, lease, HITL inbox, audit
│   └── isv_product/         # demo project of the bootstrapper
└── infra/                   # L0 as code (M2): OpenBao, Gitea, OpenProject, k3s+Argo, LiteLLM
```

## 6. Human-in-the-Loop

Destructive actions (prod deploys, DB migrations, direct pushes to main, rights/secrets) — only with explicit human approval (spec §7.2.4, §7.7).

## 7. Localization

Source code, docs and comments — **English only**. Russian texts live exclusively in `*.ru.md` files (README.ru.md, INSTALL.ru.md, TUTORIAL.ru.md, TODO.ru.md, ARCH.ru.md, AGENTS.ru.md).

## 8. Verification Commands

Before reporting "done", run and show the output (evidence before assertions):

```bash
uv run ruff check .
uv run mypy packages apps
uv run pytest
```

On the Windows stand prefix each with `./infra/scripts/toolbox.sh` (§10).

## 9. Git discipline — incident lessons (no exceptions)

These rules come from real incidents on this branch (a 24 MB PDF in history, a sqlite artifact in two commits, accumulated conflicts from three consecutive cherry-picks):

1. **Never `git add -A`.** Explicit paths only (`git add path/...`). Artifacts (`agent-api.db`, `input-docs/`, `.vault-root-token`, unseal keys) — into `.gitignore` BEFORE starting work.
2. **No cherry-picks between branches** to "move visibility now". Moving work — a regular `git merge` (PR flow); small docs — a direct commit to main. The accumulated conflicts were the result of three consecutive cherry-picks.
3. History is rewritten only **before publication** (push to a public repo): filter-branch is acceptable for cutting out secrets/artifacts; after publication — an ADR plus direct fixes only.
4. Never run two instances of the bootstrapper/scripts in parallel (a race wiped secrets — the litellm-master case). Before re-running: check `ps` for lingering processes.
5. Binary work artifacts (test databases, PDFs) never enter git.
6. Before any git operation — verify `git status` and `git branch --show-current`; branch transitions — via `checkout -b`/`checkout`.
7. Integrations — via **merge** only; never move work between branches by cherry-pick.

## 10. Stand environment (owner directive, 2026-11-14)

The live stand runs on **Rancher Desktop's built-in Kubernetes** (k3s, moby engine) on the Windows workstation — do NOT create a k3d cluster and do NOT set up a separate WSL distro. Run every bootstrap/flow/gate command through the toolbox container from Git Bash: `./infra/scripts/toolbox.sh <cmd>` (it sets `SWARM_CLUSTER=rancher-desktop`). Details — INSTALL.md "Windows (Rancher Desktop + toolbox)".

**LLM (owner directive, 2026-11-15):** the stand has no GPU — no local inference, no Ollama. LiteLLM routes every alias (`qwen3` = tool-calling agent model, `qwen-coder` = light model, `reviewer` = bot reviewer) to the **z.ai GLM Coding Plan**. **OpenRouter is used ONLY when the z.ai plan limit is exhausted** (z.ai answers HTTP 429; it is then parked for 5 min and the OpenRouter fallbacks carry the traffic). Any other z.ai error (5xx, timeout, 400, auth) is retried on z.ai and surfaced to the caller — never rerouted to OpenRouter. LiteLLM's own `fallbacks` fire on any error, so the proxy callback `infra/litellm/zai_limit_gate.py` disables them per request until that 429; do not add an alias that reaches OpenRouter directly (`infra/litellm/deployment.yaml`, guarded by `apps/bootstrap/tests/test_litellm_manifest.py`). Local secrets live in the repo-root **`.env`** (gitignored; template `.env.example`: `ZAI_API_KEY=`, `OPENROUTER_API_KEY=`); every `litellm` bootstrap run writes the keys set there into OpenBao `secret/litellm/{zai,openrouter}`, which feeds the cluster Secret (`bootstrap.sh llm-key [zai|openrouter]` imports without redeploying). Never commit keys, never print them to logs or chat — check presence, not value. After a Rancher Desktop restart OpenBao comes up sealed: run `./infra/scripts/toolbox.sh bash infra/scripts/bootstrap.sh vault` (idempotent unseal) before any other section.

## 11. Self-development workflow (spec §13.7 level 2)

The platform develops itself through its own corridor. GitHub `kryadov/ai-based-isv` stays canonical; the lab Gitea repo `swarm-lab/ai-based-isv` is the working copy agents commit to.

1. `bash infra/scripts/platform-sync.sh pull` (host Git Bash) — fast-forward Gitea `main` from GitHub; it refuses while Gitea holds unpromoted commits.
2. File the task as a Gitea issue on `swarm-lab/ai-based-isv` and run it: `./infra/scripts/toolbox.sh bash infra/tests/selfdev_flow.sh` (task text via `SELFDEV_*` env).
3. The agent works as the NON-admin `swarm-agent`; the PR passes the platform check profile (`.gitea/workflows/ci.yml` — same gates as GitHub CI plus gitleaks/semgrep), the bot review and fix rounds.
4. **HITL on every merge** — a human (or the owner-authorized operator) reads the diff and approves the printed HITL request; auto-merge is off for this repo (`platform_repos`, `.platform/merge-policy.yaml`).
5. `bash infra/scripts/platform-sync.sh promote` → GitHub PR `selfdev/<sha7>` → GitHub CI green → merge (never a direct push to GitHub `main`) → `platform-sync.sh pull`.

Agents never get the Gitea admin token, and `.gitea/**` / `.platform/**` are protected gate paths: changes to the corridor itself come from the human-agent pair through GitHub PRs.
