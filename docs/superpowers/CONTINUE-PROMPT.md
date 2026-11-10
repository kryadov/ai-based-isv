# Continuation prompt (Windows workstation, 2026-11-10)

> Paste this as the FIRST message of the agent session on the new workstation.
> Short version at the bottom, for context-limited sessions.

## Full version

```text
Прочитай сначала: STATE.md (там warning про workstation handoff) →
docs/superpowers/HANDOFF-2026-11-10.md (контекст сессии, решения, уроки для Windows) →
AGENTS.md (правила, §9 git-дисциплина) → TODO.md и ARCH.md.

Статус: M4a+M4b DONE (PR #7, #21). Стенд на этой машине НЕ переносится —
подними его заново: uv run org-bootstrap apply, затем прогоны регрессий
./infra/tests/m2d_flow.sh (бутстрап E2E), потом m2a, m2b, m2c, m3, m4a, m4b.
Скрипты башевые — нужен WSL2 с docker+k3d. Модели субагентов — через
openrouter (opencode-go упирается в usage limit).

Следующий инкремент — M4c по готовому плану
docs/superpowers/plans/2026-11-11-m4c-mcp-servers.md (6 задач, release v0.4.0
закрывает весь M4). Если регрессии зелёные на новом стенде — начинай M4c:
план уже согласован, исполняй последовательно сам в сессии (executing-plans,
как в M4b), без параллельных субагентов; финальное ревью ветки — один
свежий ревьюер. STATE.md обнови в конце инкремента.
```

## Short version (context-limited sessions)

```text
Bootstrap: STATE.md → docs/superpowers/HANDOFF-2026-11-10.md → план
docs/superpowers/plans/2026-11-11-m4c-mcp-servers.md. Стенд пересоздать
(org-bootstrap apply + регрессии m2d, m2a-d, m3, m4a, m4b — WSL2/bash).
Потом исполняй M4c последовательно сам (executing-plans), release v0.4.0 в конце.
```
