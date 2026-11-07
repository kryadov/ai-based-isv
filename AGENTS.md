# AI-Native ISV — Engineering Guidelines for AI Agents

Welcome to the **AI-based Independent Software Vendor (AI-Native ISV)** repository.
In this organization, AI agents are primary engineering participants, working collaboratively with human leads.

## 1. Prime Directive: Superpowers Methodology

Every AI agent operating in this repository **MUST** adhere to the Superpowers framework (located in `.agents/skills/` and `.agents/rules/superpowers.md`):

1. **Check and Invoke Skills First**:
   - `brainstorming`: Always run before creating features, specs, or designs.
   - `writing-plans` / `executing-plans`: For multi-step implementation tasks.
   - `test-driven-development`: Red-Green-Refactor loop is mandatory for all code additions.
   - `systematic-debugging`: For investigating bugs or test failures.
   - `verification-before-completion`: Always run verification commands before reporting success.

2. **Quality Over Speed**:
   - Never write speculative or untested code.
   - Keep tasks bite-sized (focused on 1 component or function at a time).
   - Never skip tests or disable linters/type-checkers to bypass errors.

## 2. Architecture & Tech Stack

- **Monorepo Manager**: `uv` (Workspaces). Fast, deterministic Python package management.
- **Python Version**: `>=3.12`
- **Typing & Validation**: `pydantic>=2.0`, `mypy --strict`.
- **Formatting & Linting**: `ruff` (format and check).
- **Communication Protocol**: Model Context Protocol (MCP) for tools and integrations.
- **Human-in-the-Loop**: Destructive actions (deploys, migrations, main branch direct push) require approval.

## 3. Repository Structure

```
├── .agents/                 # Workspace-level skills, rules, and agent definitions
│   ├── rules/               # Agent behavioral and coding rules
│   └── skills/              # Superpowers and custom workflow skills
├── docs/                    # Living architecture and specs
│   ├── adr/                 # Architecture Decision Records
│   └── architecture/        # System diagrams and domain models
├── packages/                # Shared libraries and core components
│   ├── isv_core/            # Domain models, event contracts, telemetry
│   ├── agent_runtime/       # Orchestrator, state graph, approval gates
│   ├── context_engine/      # Codebase indexing, AST graph, semantic memory
│   ├── mcp_servers/         # MCP tool servers (sandboxes, git, storage)
│   └── evals/               # Agent evaluation benchmarks and quality checks
└── apps/                    # Standalone deployable services
    ├── agent_api/           # Management API & Human-in-the-loop dashboard backend
    └── isv_product/         # Target SaaS product starter service
```

## 4. Verification Commands

Before reporting any work as finished, run:
```bash
uv run ruff check .
uv run mypy packages apps
uv run pytest
```
Evidence before assertions: verify and show command output.
