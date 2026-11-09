# AI-Native ISV — Swarm-платформа агентской разработки

Организация, в которой **рои AI-агентов разрабатывают, внедряют и поддерживают ПО**.
Цель организации — полный жизненный цикл проектов — продуктов, внутренних сервисов,
тулов и ассистентов: **заведение → разработка → внедрение → поддержка** (спека §13).

Конкретно это значит:

- **Вход — задача в трекере.** Выход — изменение в production: тесты всех уровней,
  security-сканы, code review, канареечный выкат.
- **Сворм-исполнение**: координатор планирует граф задач; специалисты (архитектура,
  security, тесты) ревьюят; исполнители реализуют вершины в изолированных k3s-песочницах.
  На участок кода — один писатель; ограничение масштаба — пропускная способность
  проверки, а не генерации.
- **Gates везде**: CI без обходных путей, бот-ревью + human review, evidence pack
  к каждому merge, детерминированный канареечный анализ.
- **Память организации**: уроки, reuse-каталог и «конституция» общие для несвязанных
  проектов; project memory — у каждого репо.
- **Automations (регулярные workflow)**: сканы уязвимостей, обновления зависимостей,
  анализ логов, обработка фидбека кастомеров — тот же swarm-движок, триггеры
  расписание/событие, результаты — в inbox человека на решение.

> English version: [README.md](README.md)

## Текущее состояние

**Фундамент L0 жив и проверен на одном ноутбуке** (k3d/k3s): OpenBao,
Postgres+pgvector, Gitea (+act_runner, трекер v0 через Issues), OpenProject,
Argo CD, Prometheus/Grafana, Loki+Alloy, LiteLLM-шлюз над Ollama — разворачивается
с нуля одной идемпотентной командой, четыре end-to-end acceptance-флоу PASS.
Авторитетный статус и следующие майлстоуны — [STATE.md](STATE.md).

## Слои

| Слой | Что | Где |
|---|---|---|
| **L0** | инфраструктура организации как код: Gitea, OpenProject, OpenBao, Postgres, Argo CD, LiteLLM, наблюдаемость | `infra/` + `apps/bootstrap` |
| **L1** | платформа агентов: swarm control plane, память, quality gates, evidence pack | `packages/`, `apps/agent_api` |
| **L2** | сквозная доставка: задача из трекера → prod | конвейер, целевые проекты |

## Как запустить (dev)

```bash
git clone https://github.com/kryadov/ai-based-isv && cd ai-based-isv
uv sync --all-packages
uv run ruff check . && uv run mypy packages apps && uv run pytest
```

Acceptance-флоу L0-стенда: `./infra/tests/m2a_pr_flow.sh`,
`./infra/tests/m2b_deploy_flow.sh`, `./infra/tests/m2c_flow.sh`,
`./infra/tests/m2d_flow.sh` (детали в [INSTALL.ru.md](INSTALL.ru.md)).

## Карта проекта

| Файл | Назначение |
|---|---|
| [STATE.md](STATE.md) | **ground truth: состояние, майлстоуны, следующий шаг** — читать первым в каждой сессии |
| [AGENTS.md](AGENTS.md) | правила для агентов (мандат Superpowers, стандарты) |
| [INSTALL.ru.md](INSTALL.ru.md) | установка: prerequisites, dev-окружение, L0 bootstrap |
| [TODO.ru.md](TODO.ru.md) | бэклог и обязательные петли |
| docs/superpowers/specs/ | полный дизайн платформы + аддендумы |
| docs/adr/ | архитектурные решения (стек, Executor, память) |
| docs/superpowers/plans/ | планы реализации по инкрементам |
