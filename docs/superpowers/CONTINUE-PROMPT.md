# Continuation prompt (new Windows workstation, no GPU, 2026-11-12)

> Paste this as the FIRST message of the agent session on the new workstation.
> Short version at the bottom, for context-limited sessions.

## Full version

```text
Прочитай сначала: STATE.md → docs/superpowers/HANDOFF-2026-11-12.md (контекст
сессии, все rulings, находки финального ревью, план дальше) → AGENTS.md (§9
git-дисциплина) → план docs/superpowers/plans/2026-11-11-m4c-mcp-servers.md.

Работа идёт в ветке feat/m4c-mcp-servers (git fetch && git checkout
feat/m4c-mcp-servers). M4c Tasks 1–6 реализованы, m4c_flow PASS на старом стенде;
из финального ревью исправлен Important #1, осталось Important #2+#3 (read-only
task-reader для песочниц, write task-source только с хоста, netpol from:) —
дизайн в HANDOFF, раздел "Next steps".

Новая машина: без GPU, 32 GB. Стенд не переносится — подними заново по
INSTALL.md "Windows (WSL2)" (.wslconfig: memory=24GB + inotify-строка), затем
uv run org-bootstrap apply. LLM переводим на внешнюю модель через Ollama
(решение владельца) — локально на CPU qwen3 для агентских флоу не гоняй.
Сначала согласуй со мной конкретный endpoint/модель и как хранить ключ (OpenBao,
не git), потом подключи его в LiteLLM под алиасом qwen3 (или параметризуй MODEL
во флоу) и проверь префлайт tool_calls.

Дальше: фиксы Important #2+#3 по TDD → гейты → m4c → регрессии m2d, m2a, m2b,
m2c, m3, m4a, m4b → STATE.md (M4c DONE) → PR в main. Мерж и релиз v0.4.0
(./infra/scripts/release.sh v0.4.0) — только с моего подтверждения.
Исполняй последовательно сам (executing-plans), без параллельных субагентов.
```

## Short version (context-limited sessions)

```text
Bootstrap: STATE.md → docs/superpowers/HANDOFF-2026-11-12.md. Ветка
feat/m4c-mcp-servers. Стенд пересоздать (INSTALL Windows/WSL2, org-bootstrap apply),
LLM — внешняя модель через Ollama (endpoint согласовать со мной). Затем фиксы
ревью I2+I3 → гейты → m4c + регрессии → STATE → PR. Мерж/релиз v0.4.0 — с моего ок.
```
