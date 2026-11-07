# Superpowers Agent Rules

All AI agents working within this repository must adhere to the **Superpowers Engineering Methodology**.

## Core Principles

1. **Invoke Skills Before Action**:
   - If a task involves creating features, building components, or designing architecture, invoke `brainstorming` first.
   - If a task involves fixing bugs or investigating failures, invoke `systematic-debugging` first.
   - For execution, follow `executing-plans` or `subagent-driven-development`.

2. **No Speculative Code**:
   - Never generate unverified code dumps.
   - Always operate in bite-sized, incremental tasks with clear specifications.

3. **Test-Driven Development (TDD)**:
   - Write unit or integration tests *before* writing implementation code (Red).
   - Verify the test fails for the expected reason.
   - Implement the minimal code required to pass (Green).
   - Refactor cleanly with passing tests (Refactor).

4. **Evidence Before Assertions**:
   - Before claiming any task is complete, run the automated tests and type checks (`verification-before-completion`).
   - Never claim tests pass without running them in the environment and showing output.

5. **Branch & Worktree Isolation**:
   - Non-trivial feature work should be isolated in git branches / worktrees to protect `main`.
