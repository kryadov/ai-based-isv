# Research: model subscriptions, continuity and role-based routing

Date: 2026-11-14 · Status: research (input for a future ADR, e.g. "ADR-000x: model routing and provider budget") · Author: research agent for the owner

Scope: how to keep development **continuous** across rate/usage windows and how to put the **right model on each kind of work**, both for the owner's interactive work today (Phase 1) and for the platform's own agent roles later (Phase 2, spec §13.7 recursive self-development). Grounded in repo facts: executor = OpenCode in k3s sandboxes (ADR-0002), PydanticAI for light reasoning roles, LiteLLM gateway at `infra/litellm/deployment.yaml` (today: two local Ollama models), keys live in OpenBao.

Conventions: every price/limit was checked on 2026-11-14 unless marked otherwise. **UNVERIFIED** = could not be confirmed from a primary source; treat as indicative. Plans and model line-ups in this market change monthly — re-check before acting on any number.

---

## TL;DR

1. **Split by ToS, not by taste.** Subscriptions (Claude Pro, z.ai GLM Coding Plan, OpenCode Go) are for *their supported harnesses*; metered APIs (OpenRouter, Anthropic API key, Ollama Cloud pay-as-you-go, z.ai pay-as-you-go API) are for *the platform* and anything going through LiteLLM. Claude Pro OAuth outside Claude Code/claude.ai is explicitly prohibited and enforced; z.ai's plan is limited to a list of supported tools that does not include LiteLLM.
2. **Phase 1 (now):** Claude Code on Pro with **Opus 5.5 for planning/spec/review only**; bulk implementation and test writing go to **GLM-5.3** (z.ai plan) via a second Claude Code profile or OpenCode; **OpenCode Go** (Kimi K3.1 Pro, GLM-5.3, DeepSeek V4, MiniMax M3.1 Flash, even Claude Haiku 5.5) is the second fallback; **OpenRouter** is the always-on last resort. Switching is a shell alias, not a reconfiguration, and the repo's STATE.md/HANDOFF discipline makes switching harness/model mid-increment cheap.
3. **Phase 2 (platform agents):** LiteLLM exposes **role aliases** (`role-planner`, `role-implementer`, `role-reviewer`, `role-tests`, `role-cheap`, `role-embed`) with ordered fallbacks across *metered* providers, cooldown-on-429, per-key budgets, and the resolved model written into the run manifest. Subscriptions are not routed through LiteLLM.
4. **Continuity rule:** implementer/test/triage roles may *fall back*; gate roles (reviewer, spec self-eval judge, final review) must *fall back only within the same quality tier, otherwise queue and wait* — never silently degrade a gate to a cheap model.
5. **Reviewer ≠ implementer family.** Use cross-family review (GLM implements → Kimi/Claude reviews), it is the cheapest quality lever available.
6. **Embeddings stay local** (Ollama on the stand) or `qwen/qwen3-embedding-8b` on OpenRouter ($0.01/M) — but pin one model forever per index; pgvector dims must not change silently via fallback.

---

## 1. Resources (as of 2026-11-14)

| Resource | Price | Limits | Models | Harnesses that may consume it | Automation / proxy ToS |
|---|---|---|---|---|---|
| **Claude Pro** (Claude Code) | $20/mo | Rolling 5-hour window + weekly cap, **shared** with claude.ai chat and desktop; exact numbers not published (Anthropic publishes multiples). May 2026: Claude Code 5-hour limits doubled; post-promo weekly limits reported +25% vs pre-promo (UNVERIFIED secondary sources). `/usage` shows state. | Opus 5.5 (default in Claude Code ≥2.1.280 on Pro), Sonnet 5.5, Haiku 5.5. Fable models are not in Pro included usage. | **Only** Claude Code and claude.ai/native Anthropic apps. | OAuth "intended exclusively for … ordinary use of Claude Code and other native Anthropic applications". Using Pro/Max credentials in third-party harnesses (OpenCode, Cline, LiteLLM, own Agent-SDK apps) is prohibited and has been technically blocked/enforced since Jan–Apr 2026. Advertised limits "assume ordinary, individual usage of Claude Code and the Agent SDK". Headless `claude -p` by the owner for own work is inside Claude Code → OK; a 24/7 agent pool on the owner's subscription → **risky** (see §6). |
| **z.ai GLM Coding Plan** | From $18/mo (Lite). Pro/Max prices conflict across sources: $72/$160 or $80/$168 (UNVERIFIED; check z.ai/subscribe). Legacy plans not sold since 2026-07-30. | Credits: Lite 2,000 per 5h / 10,000 per week; Pro 12,000 / 60,000; Max 28,000 / 140,000. Off-peak charged at 50%; peak = weekdays 14:00–18:00 UTC+8 (= 09:00–13:00 Moscow / 06:00–10:00 UTC). Concurrency by tier (Lite ≈ one project at a time). | GLM-5.3, GLM-5.3-Flash (requests for 5.2/5.1 → 5.3, 4.7 → 5.3-Flash). | Official list: ZCode, **Claude Code**, Claude for IDE, Codex, **OpenCode**, Pi, Cursor, Cline, TRAE, Qoder, Droid, Kilo Code, Roo Code, Crush, Goose, Eigent; best-effort: OpenClaw, Hermes Agent, etc. Endpoints: Anthropic `https://api.z.ai/api/anthropic`, OpenAI chat `https://api.z.ai/api/coding/paas/v4`. | "May only be used within officially supported tools"; unsupported tools "may result in restricted benefits". No account sharing. Violations → rate limiting/freeze; >3 violations → ban. **LiteLLM / custom gateways are not listed → risky.** OpenCode-in-k3s is a supported tool, but swarm concurrency above the tier's guidance is risky. |
| **OpenCode Go** | $10/mo (Go), $40 (Go Plus), $20/member (Teams) | Per-model monthly $ budget, split into 5h (20%), week (50%), month (100%) windows (e.g. Go: GLM-5.3 $15/mo, MiniMax M3 $60, DeepSeek V4.1 Flash $100). On exhaustion: blocked, or pay-as-you-go if "Extra Usage" + Console balance; free models remain. Each response reports cost in `opencode.allowance`. | 34 models incl. **Kimi K3.1 Pro**, Kimi K3, Kimi K2.7 Code, GLM-5.3/5.3-Flash/5.2, **DeepSeek V4 Pro / V4.1 Flash**, Qwen3.8 Max/Flash, Qwen3.7 Plus, MiniMax M3 / M3.1 Flash, MiMo, **Claude Haiku 5.5**, Grok 4.7, GPT 6 Luna, Gemini 4 Argon. List changes; authoritative: `https://opencode.ai/inference/go/v1/models`. | OpenCode natively; endpoints may be called directly (OpenAI chat/responses, Anthropic messages, Google); validated clients incl. Claude Code, Codex, Kilo Code. | "Designed for OpenCode and other coding agents". Clients must send *typical coding-agent traffic*, their own user agent (not a generic SDK name) and a stable `x-opencode-session` header; traffic is monitored for abuse; **service accounts are not covered**. → interactive + OpenCode executor: OK; behind LiteLLM as a generic backend: **gray/risky**. |
| **OpenRouter** | Pay-as-you-go; 5.5% fee on credit purchases ($0.80 min); BYOK free up to $25k/mo | Rate limits per account (not quantified in FAQ); free models 50 req/day (<10 credits bought) or 1,000/day | Everything: Claude (Anthropic 1P), GLM-5.3 (z.ai 1P $1.40/$4.40), Kimi K3.1 Pro ($0.24–$4.00 in / $8.4–$20 out by provider), DeepSeek V4 Pro (~$0.70–$1.91 / $1.39–$3.83), DeepSeek V4.1 Flash (~$0.05–$0.30 / $0.20–$1.20), MiniMax M3.1 Flash ($0.075 / $0.30), qwen3-embedding-8b ($0.01/M) | Anything: LiteLLM (`openrouter/…`), OpenCode, Claude Code (`ANTHROPIC_BASE_URL=https://openrouter.ai/api`), PydanticAI | API product — automation and proxying are the intended use. Use provider routing (ZDR / data-policy filters, EU endpoint) for privacy. Cheapest providers for a model may quantize or be less reliable — pin providers for gate roles. |
| **Ollama Cloud** (optional; stand is moving there) | Pay-as-you-go $0 base; Pro $20 incl. $60 credits; Max $100 incl. $300 | Token-metered since 2026-08-31 (no 5h/weekly caps on new plans); concurrency 3 (PAYG/Pro) / 10 (Max); off-peak discount outside weekdays 12:00–18:00 UTC | glm-5.3 $1.40/$4.40, glm-5.3-flash $0.15/$0.50, deepseek-v4-pro $1.32/$3.96, deepseek-v4.1-flash $0.30/$1.20, kimi-k3 $3/$15, minimax-m3 $0.60/$2.40, gpt-oss … | Ollama API / OpenAI-compatible API; LiteLLM `ollama_chat/` | Metered → automation is the expected use; formal ToS text not reviewed (**UNVERIFIED**). Concurrency 3 is the practical swarm bottleneck on Pro. |
| **Anthropic API key** (not owned yet; recommended for Phase 2 gates) | Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Haiku 5.5 $0.10/$0.50 per 1M (Haiku ≤100K-token prompts); cache reads ~0.1× | Tiered rate limits | Full Claude line | Anything incl. LiteLLM, PydanticAI, Agent SDK | Commercial Terms; the sanctioned way to use Claude in a platform. Also reachable via OpenRouter (Anthropic 1P) without a separate account. |

---

## 2. Which harness can consume which resource

| Harness | Claude Pro | z.ai plan | OpenCode Go | OpenRouter | Ollama Cloud | Notes |
|---|---|---|---|---|---|---|
| **Claude Code** (native login) | ✅ native | — | — | — | — | The only legitimate consumer of Pro. |
| **Claude Code** + `ANTHROPIC_BASE_URL` | — | ✅ `https://api.z.ai/api/anthropic`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL=glm-5.3[1m]/glm-5.3-flash[1m]` | ✅ (validated client; Anthropic endpoint `…/inference/go/anthropic`) — UNVERIFIED for non-Anthropic-native models via that path | ✅ `https://openrouter.ai/api`, empty `ANTHROPIC_API_KEY`, `ANTHROPIC_DEFAULT_*_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` | via LiteLLM only | Use a **separate `CLAUDE_CONFIG_DIR`** per profile so the Pro OAuth login is not clobbered (OpenRouter docs say you must `/logout` otherwise). Claude Code is tuned for Claude; non-Claude models work but tool-use quality varies. |
| **OpenCode** | ❌ prohibited | ✅ provider `zai-coding-plan` (baseURL `https://api.z.ai/api/coding/paas/v4`; some versions use `zhipuai`) | ✅ native (`opencode/…` Go models) | ✅ native provider | ✅ (OpenAI-compatible provider) | Reads `AGENTS.md` natively → the repo rules travel with it. This is also the Phase-2 executor, so Phase-1 use doubles as executor dogfooding. |
| **LiteLLM** (platform gateway) | ❌ prohibited | ⚠️ not a supported tool → risky; use z.ai **pay-as-you-go API** instead | ⚠️ gray (needs per-session header, coding-agent traffic) | ✅ | ✅ | Phase-2 central router for *metered* providers. |
| **claude-code-router (CCR)** | ❌ (Pro OAuth can't go through it) | ⚠️ same as LiteLLM | ⚠️ | ✅ | ✅ | Local proxy in front of Claude Code with per-scenario routing and fallback chains (now SQLite config + `ccr ui`; syntax changed recently — UNVERIFIED). Only worth it if you want automatic fallback *inside* one Claude Code session; aliases + handoff are simpler. |
| **PydanticAI** (light roles) | ❌ | ❌ | ⚠️ | ✅ via LiteLLM | ✅ via LiteLLM | Always through LiteLLM (single entry point, spec §7.8). |

---

## 3. Role → model mapping

Evidence caveat: SWE-bench Verified is saturated (Vals.ai stopped running it; top scores ~96–97% for Opus 5 / DeepSeek V4 Pro under mini-SWE-agent). More discriminating, but still noisy: Terminal-Bench 2 (BenchLM 2026-09-15 snapshot: top open-weight GLM-5.2 81.0%; DeepSeek V4 Pro 63.3%, Kimi K2.6 66.7% in an earlier snapshot) and SWE-bench Pro (Scale standardized, Sep 2026: best open-weight ~62% — Qwen3.8-Flash-Next 62.5%, GLM-5.2 62.1% vendor-aggregate; Opus 4.8 69.2% vs DeepSeek V4 Pro 55.4% in a direct comparison). Scores for GLM-5.3, Kimi K3.1 Pro, Opus/Sonnet 5.5 on standardized boards: **UNVERIFIED**. Practical evidence from this repo: `minimax-m3.1-flash` and `kimi-k3.1-pro` "proved good for reviews and fixes" (HANDOFF-2026-11-10). **Conclusion: Claude still leads on long-horizon agentic work and judgment; GLM-5.x is the strongest open-weight agentic coder; the gap is large enough to keep Claude at the gates, small enough to give bulk implementation to GLM/Kimi.** The platform's own eval suites (`packages/evals`) should replace public benchmarks for routing decisions as soon as they exist (see §7).

| Role (platform) / activity (owner) | Primary | Fallback chain (same tier first) | Why |
|---|---|---|---|
| **Planner / spec / architecture / ADRs** (brainstorming, writing-plans) | Opus 5.5 (Phase 1: Claude Pro; Phase 2: Anthropic API key or OpenRouter Anthropic 1P), effort `high` | Kimi K3.1 Pro → GLM-5.3 → *queue* | Highest-leverage tokens; errors here multiply across the swarm. Low volume, so even metered Opus is cheap. |
| **Implementer** (coding vertex in OpenCode) | GLM-5.3 (z.ai plan via OpenCode; Phase 2 metered: OpenRouter z.ai 1P or Ollama Cloud) | Kimi K3.1 Pro → DeepSeek V4 Pro → Sonnet 5.5 | Bulk tokens; GLM-5.x top open-weight on agentic boards; tests + review gates catch residual errors. |
| **Complex implementation** (cross-package refactors, concurrency, k8s/DBOS subtleties, debugging after 2 failed attempts) | Sonnet 5.5 (Phase 1: Pro; Phase 2: API) | Opus 5.5 → Kimi K3.1 Pro | Escalation tier; trigger it by policy (attempt count, diff size, touched paths), not by default. |
| **Test writer** (TDD RED step, fixtures, acceptance scripts) | GLM-5.3-Flash / DeepSeek V4.1 Flash | MiniMax M3.1 Flash → GLM-5.3 | High volume, well-specified; the "done_when" contract constrains it. Keep implementer and test writer in different families when tests are the gate. |
| **Reviewer** (PR bot-reviewer, verification-before-completion) | Different family from the implementer: Kimi K3.1 Pro if code came from GLM; Sonnet 5.5 for platform-critical paths (`apps/agent_api`, `infra/`, gates) | Same-tier only (Sonnet 5.5 ↔ Kimi K3.1 Pro ↔ GLM-5.3 when implementer was not GLM) → *queue* | Cross-family review catches correlated blind spots; a gate must not degrade silently. |
| **Final review before merge to main of the platform** (spec §13.7 hardening) | Opus 5.5 + human (HITL) | *queue* (no fallback) | Recursion safety: the platform reviewing its own gates. |
| **Spec self-eval / LLM judge** (PydanticAI) | Sonnet 5.5 or Kimi K3.1 Pro — **never the same model as the generator** | same tier → queue | Judge drift silently corrupts evals; pin judge model+version in the run manifest. |
| **Triage / classification / routing** (issue labels, severity, "which role?") | Haiku 5.5 (API, $0.10/$0.50) or MiniMax M3.1 Flash ($0.075/$0.30) | DeepSeek V4.1 Flash → local qwen3 | Structured output, tiny prompts; local model as zero-cost floor. |
| **Summarization / session-log digests / handoff drafts / memory extraction** | DeepSeek V4.1 Flash or Haiku 5.5 | MiniMax M3.1 Flash → GLM-5.3-Flash → local qwen3 | Long input, cheap output; quality needs are moderate; consolidation of org memory (ADR-0003) one tier up (GLM-5.3) because errors persist. |
| **Embeddings / memory index** | Local Ollama embedding model on the stand (zero cost, private), or `qwen/qwen3-embedding-8b` on OpenRouter ($0.01/M, 32K ctx) | **None across models** — same model on another provider only | Changing the embedding model changes vector space/dims; fallback to a different model would corrupt pgvector search. Re-embed via an explicit migration instead. |
| **Owner's quick questions / chores in the terminal** | Haiku 5.5 or GLM-5.3-Flash | anything cheap | Don't burn Opus window on `git status`-level work. |

---

## 4. Continuity mechanics

### 4.1 Interactive (Phase 1)
- **Profiles as aliases** (no runtime switching inside one session):
  - `cc` → Claude Code, Pro login (default config dir).
  - `cz` → `CLAUDE_CONFIG_DIR=~/.claude-zai claude` with `env` in that dir's `settings.json`: `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`, `ANTHROPIC_AUTH_TOKEN` from the OS keychain/env (never committed), model vars → `glm-5.3[1m]` / `glm-5.3-flash[1m]`.
  - `cr` → `CLAUDE_CONFIG_DIR=~/.claude-openrouter claude` with OpenRouter env (Kimi K3.1 Pro / DeepSeek V4 Pro / Claude via Anthropic 1P when Pro is exhausted but a Claude-quality step is required).
  - `oc` → OpenCode with providers `opencode` (Go), `zai-coding-plan`, `openrouter`; switch with `/models`.
- **Trigger** to switch: Claude Code reports the window is exhausted, or `/usage` shows >80% of the 5h window during a planning step you can't finish. Planning work waits for the window reset (or goes to `cr` with Opus metered); implementation continues on `cz`/`oc`.
- **Handoff** (the repo already has the pieces: `STATE.md`, `docs/superpowers/HANDOFF-*.md`, `CONTINUE-PROMPT.md`, plans in `docs/superpowers/plans/`):
  - Work in plan-sized tasks; tick checkboxes in the plan file as you go — the plan is the ledger.
  - At each switch: WIP commit on the feature branch (explicit paths, AGENTS.md §9), then a 5-line checkpoint appended to the current HANDOFF (task id, last green command, next step, open doubts). The receiving harness starts from `CONTINUE-PROMPT.md` → STATE → plan.
  - Because both Claude Code and OpenCode read `AGENTS.md` (Claude Code via `CLAUDE.md` → `@AGENTS.md`), the rules are identical across harnesses; skills in `.agents/skills/` must stay harness-neutral.
- **Spend the scarce window deliberately:** Opus for brainstorming/specs/plans and final review; `/model sonnet` for complex execution; subagents in Claude Code count against Pro too — mark exploration subagents `model: haiku` or run them on `oc`. Avoid long-context Opus sessions; `/clear` between tasks (it does not restore quota, but it lowers per-turn cost).
- **Off-peak shaping for z.ai:** schedule heavy GLM runs outside 06:00–10:00 UTC weekdays (peak = 2× cost in credits).

### 4.2 Platform (Phase 2)
- **LiteLLM router**: `order` (priority) between deployments of one alias, `fallbacks` across aliases (run after `num_retries`; cover 429/5xx), `allowed_fails` + `cooldown_time` to bench a provider that keeps returning 429, `context_window_fallbacks` for long inputs, `retry_policy` with `RateLimitErrorRetries: 0` on the subscription-like backends (fail over fast instead of hammering).
- **Budgets**: LiteLLM virtual keys per role/team with `max_budget` + `budget_duration`; the proxy re-checks budget before each fallback target and skips over-budget ones. Provider-level caps: OpenRouter key credit limits; Ollama 90% email; OpenCode Go `opencode.allowance`.
- **Gate semantics in the control plane** (`apps/agent_api`): a vertex whose role is a gate gets `degrade_allowed=false`; when every same-tier deployment is cooling down, the lease is released and the task re-queued with backoff instead of failing — continuity through *waiting*, not degradation.
- **Audit**: record `resolved_model` + provider + fallback hop count in the VET/run manifest (ADR-0002 already plans `resolved_model`); LiteLLM returns the actual model in the response and spend logs in its Postgres DB.

---

## 5. Recommended setup

### Phase 1 — now (owner drives from Claude Code)

| Work | Tool / profile | Model |
|---|---|---|
| Brainstorm, spec, plan, ADR, final review of a branch | `cc` | Opus 5.5 |
| Executing a plan task that is tricky | `cc` | Sonnet 5.5 |
| Executing routine plan tasks, TDD loops, infra script fixes | `cz` (or `oc` + `zai-coding-plan`) | GLM-5.3 |
| Second opinion / review of GLM work | `oc` (Go) | Kimi K3.1 Pro |
| Exploration, log digests, quick chores | `oc` (Go) | MiniMax M3.1 Flash / DeepSeek V4.1 Flash / Haiku 5.5 |
| Everything exhausted, or Claude-grade step can't wait | `cr` (OpenRouter) | Opus/Sonnet 5.5 (metered) or Kimi K3.1 Pro |

Expected monthly fixed cost: $20 (Pro) + $18 (z.ai Lite) + $10 (Go) = **$48** + OpenRouter usage. Upgrade order if limits bite: z.ai Lite → Pro (bulk work) before Claude Pro → Max (only if planning/review time is the bottleneck). Ollama Cloud: use pay-as-you-go (no subscription) for the stand.

Dogfooding bonus: running real plan tasks in OpenCode with `AGENTS.md` is exactly what the executor does in k3s; frictions found here are executor bugs found early.

### Phase 2 — platform runs its own agents (owner supervises)

- **LiteLLM** carries only metered backends (OpenRouter, Anthropic API key, Ollama Cloud, optionally z.ai pay-as-you-go API) and exposes role aliases; OpenCode executor sandboxes and PydanticAI roles call `role-*` aliases through LiteLLM (the existing netpol path).
- **Subscriptions stay human-side**: Claude Pro for the owner's supervision/reviews in Claude Code; z.ai plan and OpenCode Go for the owner's interactive work. Optionally (owner decision, see §6), one *low-concurrency* executor lane may use OpenCode's native `zai-coding-plan` provider directly, bypassing LiteLLM — supported tool, but must respect concurrency guidance.
- **Escalation policy** in the control plane: implementer starts on `role-implementer`; after N failed gate attempts or a diff touching protected paths → `role-implementer-strong`; reviewer alias chosen to be cross-family from the implementer's resolved model.
- **Recursion hardening** (spec §13.7): any change to `infra/litellm/` routing or role aliases is itself a gate-affecting change → HITL approval, no auto-merge.

### Config sketch (`infra/litellm/` ConfigMap, Phase 2)

Keys are never literal: `os.environ/…` values are injected from Kubernetes Secrets that the bootstrapper syncs from OpenBao (same pattern as `LITELLM_MASTER_KEY` today). Model ids below are OpenRouter slugs seen on 2026-11-14 — verify before use.

```yaml
model_list:
  # ---- role-planner: Claude-grade reasoning (gate tier) ----
  - model_name: role-planner
    litellm_params:
      model: anthropic/claude-opus-5-5          # Anthropic API key (Commercial Terms)
      api_key: os.environ/ANTHROPIC_API_KEY
      order: 1
  - model_name: role-planner
    litellm_params:
      model: openrouter/anthropic/claude-opus-5.5
      api_key: os.environ/OPENROUTER_API_KEY
      order: 2
  - model_name: role-planner-alt                  # same tier, different family
    litellm_params:
      model: openrouter/moonshotai/kimi-k3.1-pro
      api_key: os.environ/OPENROUTER_API_KEY

  # ---- role-implementer: bulk coding ----
  - model_name: role-implementer
    litellm_params:
      model: openrouter/z-ai/glm-5.3
      api_key: os.environ/OPENROUTER_API_KEY
      extra_body: {provider: {order: ["z-ai"], allow_fallbacks: true}}   # prefer 1P
      order: 1
  - model_name: role-implementer
    litellm_params:
      model: ollama_chat/glm-5.3                  # Ollama Cloud PAYG (endpoint UNVERIFIED)
      api_base: https://ollama.com
      api_key: os.environ/OLLAMA_API_KEY
      rpm: 30                                     # concurrency 3 on PAYG/Pro
      order: 2
  - model_name: role-implementer-strong
    litellm_params:
      model: anthropic/claude-sonnet-5-5
      api_key: os.environ/ANTHROPIC_API_KEY

  # ---- role-reviewer: cross-family gate ----
  - model_name: role-reviewer
    litellm_params:
      model: openrouter/moonshotai/kimi-k3.1-pro
      api_key: os.environ/OPENROUTER_API_KEY
      order: 1
  - model_name: role-reviewer-claude
    litellm_params:
      model: anthropic/claude-sonnet-5-5
      api_key: os.environ/ANTHROPIC_API_KEY

  # ---- role-tests / role-cheap / role-summarize ----
  - model_name: role-tests
    litellm_params:
      model: openrouter/z-ai/glm-5.3-flash
      api_key: os.environ/OPENROUTER_API_KEY
  - model_name: role-cheap
    litellm_params:
      model: openrouter/minimax/minimax-m3.1-flash
      api_key: os.environ/OPENROUTER_API_KEY
      order: 1
  - model_name: role-cheap
    litellm_params:
      model: anthropic/claude-haiku-5-5
      api_key: os.environ/ANTHROPIC_API_KEY
      order: 2
  - model_name: role-local                        # zero-cost floor (existing host Ollama)
    litellm_params:
      model: openai/qwen3:latest
      api_base: http://host.lima.internal:11434/v1   # keep the dynamic host resolution of section_litellm
      api_key: ollama

  # ---- role-embed: ONE model, no cross-model fallback ----
  - model_name: role-embed
    litellm_params:
      model: openai/qwen3-embedding               # local Ollama embedding model (name UNVERIFIED)
      api_base: http://host.lima.internal:11434/v1
      api_key: ollama
    model_info: {mode: embedding}

router_settings:
  routing_strategy: simple-shuffle    # `order` decides priority; shuffle only within equal order
  num_retries: 2
  allowed_fails: 3
  cooldown_time: 60
  retry_policy:
    RateLimitErrorRetries: 0          # 429 -> fail over immediately, provider cools down
    TimeoutErrorRetries: 1
    DefaultRetries: 2
  enable_pre_call_checks: true        # needed for context_window_fallbacks
  fallbacks:
    - role-planner: ["role-planner-alt"]                      # same tier only; then the control plane queues
    - role-implementer: ["role-implementer-strong"]
    - role-reviewer: ["role-reviewer-claude"]                 # gate: same tier only
    - role-tests: ["role-implementer", "role-cheap"]
    - role-cheap: ["role-local"]
  context_window_fallbacks:
    - role-cheap: ["role-implementer"]
  # deliberately NO fallback for role-embed

litellm_settings:
  drop_params: true

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  # per-role virtual keys with max_budget/budget_duration are created via the LiteLLM API
  # by the bootstrapper and stored in OpenBao (secret/litellm/keys/<role>).
```

The reviewer cross-family rule (GLM-implemented → Kimi review; Kimi-implemented → Claude review) is decided by the control plane from the implementer's `resolved_model`, not by LiteLLM.

---

## 6. Risks and open questions for the owner

1. **Claude Pro in automation (high).** Using the Pro OAuth token anywhere except Claude Code/claude.ai is prohibited and enforced (account action without notice). Running *unmodified* Claude Code headless inside the stand with the owner's own login is textually tolerated by the legal page, but the limits "assume ordinary, individual usage" — a 24/7 agent lane is exactly what the April-2026 enforcement targeted. Recommendation: never wire Pro into the platform; buy an API key (or use OpenRouter Anthropic 1P) for Claude-tier platform roles.
2. **z.ai plan through LiteLLM (medium-high).** Not a supported tool; possible "restricted benefits"/freeze. Either use z.ai pay-as-you-go API / OpenRouter z.ai 1P for the platform, or accept one OpenCode-native lane at low concurrency. **Owner decision needed** — it conflicts with "LiteLLM is the single entry point" (spec §7.8); an ADR should record the exception or the choice of metered-only.
3. **OpenCode Go behind a gateway (medium).** Needs per-session `x-opencode-session` and a real client UA; service accounts not covered. Keep Go human-side unless OpenCode confirms otherwise (worth asking them).
4. **Plan volatility.** z.ai repriced twice in 2026 (Pro/Max prices still conflicting across sources); Ollama moved to credits in Aug; OpenCode Go limits are being contested publicly; Claude limits changed in May and Sept. Mitigation: provider-agnostic role aliases + monthly re-check (add a TODO feedback loop).
5. **Quality drift from fallbacks.** A silent hop from GLM-5.3 to a cheap model changes behavior and invalidates eval comparisons. Mitigation: `resolved_model` in the manifest, gate roles same-tier-only, and evals stratified by resolved model.
6. **Data/privacy.** OpenRouter's cheapest providers for Kimi/DeepSeek may log or quantize; Muse Spark on Go allows training on requests. For platform code and org memory, set OpenRouter data-policy routing (ZDR/no-training) and pin providers for gate roles. Open question: is any client/target-project code going to flow through these providers?
7. **Benchmarks don't transfer.** Public scores for GLM-5.3 / Kimi K3.1 Pro / Opus-Sonnet 5.5 are vendor-reported or missing on standardized boards (UNVERIFIED). Open question: when will `packages/evals` have a role-level routing eval (e.g. 20 historical tasks from this repo replayed per model) — that should drive the mapping in §3.
8. **Embedding lock-in.** Choose the embedding model before the memory index grows; switching later is a re-embed migration.
9. **Open question:** is a Claude Max upgrade cheaper than metered Opus for the owner's planning/review volume? Measure a month of `/usage` + OpenRouter spend first.

---

## 7. Suggested follow-ups (for TODO.md, not done here)

- ADR "model routing & provider budgets" capturing §3 (role aliases), §4.2 (gate semantics) and the subscription/ToS split.
- Bootstrapper: sync `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OLLAMA_API_KEY` from OpenBao into the `platform` namespace; create per-role LiteLLM virtual keys with budgets.
- Control plane: `degrade_allowed` per role, re-queue on "all same-tier deployments cooling down", cross-family reviewer selection.
- Routing eval in `packages/evals`: replay N historical plan tasks per candidate model; publish cost-per-green-task.
- Owner shell profiles `cc`/`cz`/`cr`/`oc` documented in INSTALL.md (keys from keychain, never in repo).

---

## Sources (all accessed 2026-11-14)

Anthropic / Claude
- Claude Code — Legal and compliance (OAuth vs API keys, ordinary individual usage): https://code.claude.com/docs/en/legal-and-compliance
- Anthropic Consumer Terms: https://www.anthropic.com/legal/consumer-terms
- The Register, "Anthropic clarifies ban on third-party harnesses with Claude subs" (2026-02-20): https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access
- Hongkiat, "Anthropic just closed the OpenClaw subscription loophole": https://www.hongkiat.com/blog/anthropic-closed-openclaw-loophole/
- Claude usage limits guides (secondary): https://alexcloudstar.com/blog/claude-code-usage-limits-2026/ ; https://dupple.com/learn/claude-usage-limits ; https://fenji.verdent.ai/guides/claude-code-limits-doubled-may-2026
- Claude Code defaults to Opus on Pro (2.1.280): https://mixed-news.com/en/claude-code-default-model-opus-pro-team-standard-plans/ ; https://www.notebookcheck.net/Claude-Opus-5-5-is-included-with-Pro-and-Free-still-misses-out.1406452.0.html
- Claude API model prices: Anthropic pricing as cached in the claude-api reference (cache 2026-11-03); https://platform.claude.com/docs/en/about-claude/pricing

z.ai
- GLM Coding Plan overview (credits, tiers, peak hours, models): https://docs.z.ai/devpack/overview
- Usage revision notice (legacy plans, 2026-07-30 switch): https://docs.z.ai/devpack/notice/usage-revision
- Usage policy (supported tools only, sharing, bans): https://docs.z.ai/devpack/usage-policy
- Supported tools + OpenAI-compatible endpoints: https://docs.z.ai/devpack/tool/others
- Claude Code setup (base URL, model mapping): https://docs.z.ai/devpack/tool/claude
- Price-change trackers (conflicting Pro/Max prices): https://www.usagepricing.com/blueprint/activity/zhipu-ai-2026-07-22-glm-coding-plan-reprice ; https://www.morphllm.com/coding-plan
- OpenCode provider config (`zai-coding-plan`): https://docs.z.ai/devpack/open ; https://playbooks.com/skills/serejaris/ris-claude-code/opencode-config

OpenCode
- OpenCode Go docs (price, limits, models, endpoints, client requirements): https://opencode.ai/docs/go/
- Go limits feature request ($12/5h, $30/week, $60/month older figures): https://github.com/anomalyco/opencode/issues/28846

OpenRouter
- FAQ (fees, BYOK, free limits, ZDR routing): https://openrouter.ai/docs/faq
- Claude Code integration: https://openrouter.ai/docs/guides/guides/claude-code-integration
- Model pages: https://openrouter.ai/z-ai/glm-5.3 ; https://openrouter.ai/moonshotai/kimi-k3.1-pro ; https://openrouter.ai/deepseek/deepseek-v4-pro ; https://openrouter.ai/deepseek/deepseek-v4.1-flash ; https://openrouter.ai/minimax/minimax-m3.1-flash ; https://openrouter.ai/qwen/qwen3-embedding-8b

Ollama
- Pricing (PAYG/Pro/Max/Team, credits, concurrency, cloud model rates): https://ollama.com/pricing

Routing tools
- LiteLLM routing (order, cooldowns, retry policy): https://docs.litellm.ai/docs/routing
- LiteLLM reliability (fallbacks, context-window/content-policy fallbacks, budgets on fallback): https://docs.litellm.ai/docs/proxy/reliability
- claude-code-router: https://github.com/musistudio/claude-code-router ; routing docs https://musistudio.github.io/claude-code-router/docs/server/config/routing

Benchmarks (indicative, partly UNVERIFIED)
- Vals.ai SWE-bench (saturated, archived): https://www.vals.ai/benchmarks/swebench-2025-09-24
- BenchLM SWE-bench Verified / Terminal-Bench 2: https://benchlm.ai/benchmarks/swe-bench-verified ; https://www.benchlm.ai/benchmarks/terminal-bench-2
- SWE-bench Pro leaderboards: https://codingfleet.com/blog/swe-bench-pro-leaderboard-2026/ ; https://www.morphllm.com/swe-bench-pro
- Official boards to re-check: https://www.swebench.com/ ; https://www.tbench.ai/

Repo
- `docs/adr/0002-executor-opencode.md`, `infra/litellm/deployment.yaml`, `docs/superpowers/HANDOFF-2026-11-10.md` (model experience note), spec §7.8 and §13.7.
