# ARCH — how the AI-Organisation works (diagrams)

Ground truth for architecture: the spec `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md`
(§7 platform, §13 project lifecycle). This file is the visual map. Updated with each milestone.

## 1. Big picture — what the organisation does

The organization's goal: **develop, implement and support software projects**
(products, internal services, tools, assistants) with AI agent swarms doing the work
and humans holding the approval gates.

```mermaid
flowchart LR
    TRK["Tracker: task created] --> API[Control plane\nagent_api"]
    API --> SPEC[Planning agent\nspec stages + self-eval]
    SPEC -->|HITL gate 1: approve plan| HUMAN((Human))
    HUMAN --> SW[Swarm execution:\nexecutor agents in sandboxes]
    SW --> PR[Pull Request\n+ evidence pack]
    PR --> CI[CI gates: tests, security, scans]
    CI --> BR[Bot review]
    BR -->|HITL gate 2: merge| HUMAN
    HUMAN --> CD[Argo Rollouts\nCanary 5-25-50-100%]
    CD -->|analysis OK| PROD((Production))
    CD -->|degradation| RB[Auto rollback]
    MEM[("Org memory:\nlessons, reuse, constitution")] -.-> SPEC
    MEM -.-> SW
    SW -->|write-back| MEM
```

## 2. L0 stand topology (laptop, k3d/k3s — live now)

Everything provisions from scratch with `uv run org-bootstrap apply` (idempotent).

```mermaid
flowchart TB
    subgraph k3d["k3d cluster swarm-lab (k3s v1.35)"]
        subgraph platform["namespace: platform"]
            OB["OpenBao\nraft on PVC\nauto-init/unseal"]
            PG["Postgres 17 + pgvector\nCNPG operator\ndbs: gitea, openproject, litellm, swarm"]
            GT["Gitea\ngit + PR + issues + OCI registry\nNodePort 30080"]
            RUN["act_runner\ndind, PVC identity"]
            AR["Argo CD\nNodePort 30081"]
            KS["kube-prometheus-stack\nGrafana 30082 / Prometheus"]
            LK["Loki + Alloy\ndaemonset: pod logs"]
            OP["OpenProject\ntracker UI 30083\nauth broken — see TODO"]
            LL["LiteLLM gateway\n30084 → z.ai / OpenRouter"]
        end
        subgraph prod["namespace: prod"]
            PODINFO["podinfo demo\nGitOps-synced, debug logs"]
        end
        AG["agent workers\n(M3+: claim vertices)"]
    end
    ZAI["(z.ai GLM Coding Plan\nqwen3 → glm-5.3\nqwen-coder → glm-5.3-flash)"]
    ORT["(OpenRouter API — only on the z.ai limit\nglm-5.3 / deepseek-v4.1-flash / kimi)"]
    LL -->|primary, keys from OpenBao| ZAI
    LL -.->|on plan limit 429| ORT
    GH["GitHub (public)\ncanonical repo of this platform + CI"] -.-> AR
    TRK(("Tracker\n(task in)")) -.-> API
```

Ports: Gitea 30080, Argo CD 30081, Grafana 30082, OpenProject 30083, LiteLLM 30084.

## 3. Delivery pipeline — one task, end to end

```mermaid
sequenceDiagram
    participant T as Tracker (issue)
    participant API as Control plane
    participant P as Planning agent
    participant H as Human
    participant X as Executor agent (sandbox)
    participant G as Gitea
    participant C as CI + bot review
    participant D as Argo Rollouts

    T->>API: webhook/poll: task ready
    P->>P: spec stages (requirements→design→tasks)
    P->>P: spec self-eval (PASS/FAIL/WEAK)
    P->>H: HITL gate 1 — approve plan
    H->>API: approve
    API->>X: claim vertex (lease, paths_allow, done_when)
    X->>X: worktree + session log + budget metering
    X->>G: commit + PR + evidence pack
    G->>C: CI gates (tests/security/scans)
    C->>C: bot review pass
    C->>H: HITL gate 2 — human merge
    H->>G: merge
    G->>D: image build + canary 5→100%
    D-->>API: success marker / auto-rollback
    API->>API: audit trail + memory write-back
```

Scaling principle (spec §8): the bottleneck is **verification capacity** (gates/review),
not generation. Merge queue + backpressure throttle the swarm to integration speed.

**Implemented since M5 (`agent_runtime/gates.py`, verified by `m5_flow.sh`):**
- After the vertex pushes `agent/vertex-N`, the worker drives the corridor:
  PR → CI → bot review → fix rounds (≤2) → HITL merge (or policy auto-merge for docs-only) → merge.
  The tracker gets a `[platform]` comment at each stage.
- CI is the repo's **check profile**, a Gitea Actions workflow seeded by the bootstrapper:
  - job `checks`: ruff, mypy, pytest;
  - job `security`: gitleaks, semgrep.
  `main` is protected by both contexts.
- The reviewer (LiteLLM alias `reviewer`) runs on z.ai `glm-5.3` like everything else — OpenRouter is used only on the z.ai plan limit (owner directive); its limit-time fallback is a different family (`moonshotai/kimi-k3.1-pro`). The reviewer prompt, not the model family, carries the independence while on z.ai.
- Every unknown or failed state blocks the merge. Agents can never change `.gitea/**` or `.platform/**`.

```mermaid
flowchart LR
    V["vertex done<br/>branch pushed"] --> PR["PR (reused<br/>across attempts)"]
    PR --> CI{"CI checks +<br/>security"}
    CI --> RV{"bot review<br/>(cross-family)"}
    RV -->|"red CI or blocking"| FX["fix round ≤2<br/>(agent on PR branch)"]
    FX --> CI
    RV -->|"green + clean"| POL{"merge policy"}
    POL -->|"docs-only"| M["merge"]
    POL -->|"otherwise"| HITL["HITL merge<br/>approval"]
    HITL --> M
    M --> DONE["tracker Done<br/>+ PR link"]
```

### 3a. Self-development loop (spec §13.7 level 2)

The platform repo itself is a target project. GitHub stays canonical; Gitea `swarm-lab/ai-based-isv` is the agents' working copy, synced by `infra/scripts/platform-sync.sh`. Agents act as the non-admin `swarm-agent`; every merge needs HITL (no auto-merge for `platform_repos`).

```mermaid
flowchart LR
    GH["GitHub main<br/>(canonical)"] -->|"platform-sync pull<br/>(fast-forward only)"| GT["Gitea swarm-lab/ai-based-isv<br/>main"]
    GT --> TASK["issue → run → vertex<br/>(swarm-agent, sandbox)"]
    TASK --> COR["PR → platform CI → review<br/>→ HITL → merge"]
    COR --> GT
    GT -->|"platform-sync promote"| PRG["GitHub PR selfdev/&lt;sha7&gt;"]
    PRG -->|"GitHub CI green → merge"| GH
```

## 4. Swarm execution model

- **One writer per code area**; specialists review, never co-write.
- Roles: **coordinator** (plan/judge/memory rules) → **specialists** (architecture,
  security, tests) → **executors** (one vertex each, isolated worktree + pod, MCP tools).
- **brain / hands / session** separation (Pattern 19): the loop is the brain, the sandbox
  is the hands, and an append-only NDJSON **session log** survives crashes — a worker can
  die and the vertex resumes/requeues with evidence intact.
- Vertex contract: `{id, deps, paths_allow, paths_avoid, done_when, deliverable}`.
  `done_when` is validated by code, not by an LLM refusing to fail.

### 4a. Agent roles catalog

Implemented via the `identities` registry (M3): a role maps to `allowed_sections`
(gitea, ci, memory, litellm, deploy...) and to which stage of the pipeline the agent is
admitted. Humans hold separate accounts with HITL rights only.

| Role | Type | Responsibility | Pipeline stage | Tools/access |
|---|---|---|---|---|
| **planner** | specialist | task intake → spec stages, ask-questions, reuse lookup, seed tasks | before coding | tracker, memory |
| **architect** | specialist | review spec/design, invariants, reuse proposals, drift checks | spec + review | git read, memory |
| **developer** | executor | implement one vertex (code + local tests), respond to bot-review findings | execution (open-code harness) | git, gitea, ci, litellm |
| **tester** | specialist/executor | write/maintain tests, flaky hunter, coverage gaps, release-candidate acceptance runs | execution + release | git, ci, evals |
| **reviewer (bot)** | light agent | first-pass code review on PRs, dedup findings, severity labels | gates | gitea, ci read |
| **security** | specialist | SAST/dependency/secret scanning review, threat modeling, safe-harness audit | gates + automations | ci read, scanners |
| **judge** | light agent | completion judgment: `done_when` evidence check, session-log sanity — code validates, judge double-checks | per vertex + run | memory read |
| **support** | executor | support tickets, post-mortem drafts, regression reproductions | support loop | tracker, git, observability |
| **maintenance (janitor)** | executor | sweeps: dead code, orphaned tests, docs/OpenAPI drift, memory hygiene | automations | git, ci |
| **release** | executor | distribution builds: dist/archive, Docker images, checksums+SBOM (external projects, §13.5) | release channel | gitea registry, ci |

Automations reuse these roles with triggers (vuln-scan → security; dep-updates →
developer; log-analysis → support; feedback-ingest → planner). Humans: owner (HITL
decisions, memory promotion), champion (per-project config review), on-call (canary
alerts).

## 5. Memory (spec §7.4, ADR-0003)

```mermaid
flowchart TB
    subgraph project["Project memory"]
        W["working: current task state"]
        E["episodic: what was tried/failed"]
        S1["semantic: in-repo AGENTS.md/.specs"]
    end
    subgraph org["Org memory (git repo, PR-flow)"]
        CON["constitution: invariants, policies"]
        RC["reuse catalog: components + versions"]
        LE["lessons: distilled from incidents/episodes"]
    end
    E -->|consolidation + HITL promotion PR| LE
    LE -.->|retrieval slice at task start| W
    CON -.->|progressive disclosure| W
```

Write rights by role; semantic changes only via PR; "published" ≠ "improves results"
(eval-gated). TTL/consolidation guards against drift and poisoning.

## 6. Control plane internals (current state, M3)

`agent_api` (FastAPI + SQLAlchemy async):

- **runs / vertices** — task graph, atomic claim with lease TTL + heartbeat, requeue on
  failure (`UPDATE ... RETURNING`, SKIP LOCKED on Postgres).
- **identities** — agent service identities (scoped, swappable).
- **budgets** — token burn per subject, atomic limit enforcement (`spent + X <= limit`).
- **HITL inbox** — pending decision cards; one decision per request (409 on double).
- **audit** — append-only event journal (actor/action/target/outcome/payload).
- **merge queue** — entries for integration serialization (gating logic lands in M5).

Enpoints: `/runs`, `/runs/{id}/vertices`, `/vertices/claim|heartbeat|complete|fail`,
`/identities`, `/budgets/{subject}[/consume]`, `/hitl[/pending|/decision]`, `/audit`,
`/merge-queue`, `/healthz`.

## 7. Executor model (M4, designed)

OpenCode harness runs **inside a k3s pod** (initContainer clone, NetworkPolicy
allow-list to LiteLLM/MCP/Gitea only, short-lived Vault token). The worker drives it via
REST/SSE: structured vertex prompt in, event stream → session log out, diff → PR.
`done_when` is validated by our code. Permission `ask` is bridged to the HITL inbox.
The same swarm engine also runs **Automations** (scans, dep updates, log analysis,
feedback ingestion) — outputs land as Inbox cards.

## 8. Verification map

```bash
uv run ruff check . && uv run mypy packages apps && uv run pytest   # platform code
./infra/tests/m2a_pr_flow.sh        # Gitea PR-flow + branch protection
./infra/tests/m2b_deploy_flow.sh    # GitOps → prod + metrics + logs
./infra/tests/m2c_flow.sh           # tracker issues → poller + LLM gateway
./infra/tests/m2d_flow.sh           # bootstrap CLI end-to-end
./infra/tests/m3_flow.sh            # control plane on live Postgres (M3)
./infra/tests/m4a_flow.sh           # M4a executor vertex: opencode serve in docker → session log + manifest + branch in Gitea
./infra/tests/m4b_flow.sh           # M4b executor vertex in a k3s sandbox pod: NetworkPolicy + per-run Secret + NodePort, Postgres denied
./infra/tests/m4c_flow.sh           # M4c full cycle: Gitea issue → poller via task-source MCP (dedup) → run → k3s agent with MCP (tracker, llm) → branch → issue closed with verdict
```

Cross-references: full design §7.2 (pipeline), §7.3 (orchestration), §7.4 (memory),
§7.7 (HITL), §13 (project lifecycle); UI/Automations addendum 2026-11-08.
