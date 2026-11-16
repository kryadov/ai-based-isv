# INSTALL

Установка платформы и её инфраструктуры (L0). Всё разворачивается локально на ноутбуке (k3d = k3s в Docker); манифесты идентичны для любого k8s.

> English version: [INSTALL.md](INSTALL.md)

## 1. Prerequisites

| Инструмент | Мин. версия | Установка |
|---|---|---|
| Docker | 28+ | Docker Desktop / OrbStack / colima (демон должен быть запущен) |
| Homebrew | любая | [brew.sh](https://brew.sh) |
| k3d | 5.9+ | `brew install k3d` |
| helm | 4.x | `brew install helm` |
| kubectl | 1.37+ | `brew install kubectl` |
| uv | любой свежий | `brew install uv` |
| gh (опционально) | 2.x | `brew install gh` |
| psql (postgresql-client) | 14+ | `brew install libpq` / `apt install postgresql-client` (acceptance-флоу m3/m4a/m4b) |

Проверка окружения:

```bash
docker info          # демон отвечает
k3d version && helm version --short && kubectl version --client | head -1
```

Ресурсы: ~12 GB RAM под весь L0-стенд, ~20 GB диска. Порты `30080`, `30022`, `30081`, `30082` должны быть свободны.

**Windows (Rancher Desktop + toolbox) — текущий стенд (с 2026-11-14).** Кластер стенда — ВСТРОЕННЫЙ Kubernetes Rancher Desktop (k3s, движок moby): без k3d и без отдельного WSL-дистрибутива. Весь bash/Python-инструментарий запускается в toolbox-контейнере: `./infra/scripts/toolbox.sh <cmd>` из Git Bash в корне репо (например `./infra/scripts/toolbox.sh uv run pytest`, `./infra/scripts/toolbox.sh bash infra/tests/m4c_flow.sh`, без аргументов — shell). Toolbox (`infra/toolbox/Dockerfile`: uv, kubectl, helm, gh, psql, docker CLI) работает в сети хоста с docker-сокетом хоста, монтирует репо по тому же пути `/mnt/c/...`, который видит демон, и выставляет `SWARM_CLUSTER=rancher-desktop` — бутстрапер пропускает `k3d image import` (k3s делит хранилище образов moby) и `k3d --port-add` (Rancher сам пробрасывает все NodePort на localhost). Нативный Python на Windows не вариант при включённом Smart App Control (блокирует CPython от uv, DLL из wheel и `.exe`-лаунчеры uv). `toolbox.sh` также ставит `infra/rancher/swarm-lab.start` в `%LOCALAPPDATA%\rancher-desktop\provisioning\` (k3s `service-node-port-range: 10000-32767` для Postgres 15432 + лимиты inotify 1024/1048576); после установки один раз перезапустите Rancher Desktop. Traefik выключить (`rdctl set --kubernetes.options.traefik=false`). LLM: внешние API (на стенде нет GPU, локального инференса нет) — всё идёт в z.ai GLM Coding Plan; OpenRouter — ТОЛЬКО после исчерпания лимита z.ai (HTTP 429 → z.ai «остывает» 5 мин; прочие ошибки z.ai в OpenRouter не уходят — `infra/litellm/zai_limit_gate.py`). `cp .env.example .env`, заполнить `ZAI_API_KEY` и/или `OPENROUTER_API_KEY`; каждый запуск секции `litellm` пишет их в OpenBao (`secret/litellm/{zai,openrouter}`) и синхронизирует в Secret `litellm-provider` (смена ключа = правка `.env` + повторный запуск). `.env` в .gitignore — ключи в git не коммитить.

**Windows (WSL2, прежний стенд).** Бутстрапер и acceptance-флоу — bash: запускайте их внутри WSL2-дистрибутива (Ubuntu) по Windows-чекауту (`/mnt/c/...`). Проверено с Rancher Desktop (движок moby, WSL-интеграция включена для дистрибутива); k3d, helm, gh, uv, psql — внутри дистрибутива; Ollama — нативно на Windows (GPU) с `OLLAMA_HOST=0.0.0.0:11434`, из кластера доступна как `host.docker.internal` (однострочный шим `ollama` → `ollama.exe` закрывает `org-bootstrap check`). В WSL используйте отдельное uv-окружение (`UV_PROJECT_ENVIRONMENT=.venv-linux`), чтобы не затирать Windows-`.venv`, и локальный для стенда `KUBECONFIG` (Rancher симлинкает `~/.kube/config` на Windows-файл). Окончания строк принудительно LF через `.gitattributes`. Две настройки хоста обязательны: (1) поднять общий для ядра WSL2 лимит inotify — `%UserProfile%\.wslconfig` → `[wsl2]` `kernelCommandLine = sysctl.fs.inotify.max_user_instances=1024 sysctl.fs.inotify.max_user_watches=1048576` (дефолтные 128 инстансов выедает кластер, и opencode-сервер в песочнице молча виснет); (2) задать `OLLAMA_CONTEXT_LENGTH=16384` — при VRAM меньше 24 GB Ollama берёт контекст 4k по умолчанию и молча обрезает ~8k-токенный промпт harness, агент теряет инструкции.

## 2. Platform dev setup (сам монорепо)

```bash
git clone https://github.com/kryadov/ai-based-isv && cd ai-based-isv
uv sync --all-packages      # venv + все члены workspace
uv run ruff check .         # ожидание: All checks passed!
uv run mypy packages apps   # ожидание: no issues found
uv run pytest               # ожидание: все тесты зелёные
```

Python 3.12 подхватится автоматически (`.python-version`).

## 3. L0 Bootstrap (k3d + Vault + Postgres + Gitea + GitOps + наблюдаемость)

```bash
./infra/bootstrap.sh
```

Идемпотентен: повторный прогон не разрушает данные. Скрипт создаёт k3d-кластер `swarm-lab` и разворачивает **OpenBao** (raft на PVC, auto-init/unseal), Postgres (CNPG, pgvector), Gitea (NodePort `30080`, SSH `30022`, внешняя БД), act_runner, Argo CD, kube-prometheus-stack (Grafana `30082`), Loki+Alloy; административные токены/пароли кладёт в OpenBao.

Если git-репозиторий приватный, Argo CD подключается через `gh auth token` (нужен `gh auth login` или `GITHUB_TOKEN`); для публичного репо клонирует анонимно. Основной путь — CLI (`org-bootstrap`); альтернатива — `./infra/bootstrap.sh`.

Факты стенда фиксируются в `infra/env.yaml` (генерируется автоматически — артефакт VET, руками не редактировать). Секции можно гонять по отдельности: `./infra/scripts/bootstrap.sh [vault|cnpg|gitea|agent-user|runner|argocd|monitoring|loki|demo|openproject|litellm]`. **`agent-user`** (в `all` идёт сразу после `gitea`) создаёт НЕ-админского пользователя Gitea `swarm-agent` (команда `agents`, запись во все репо `swarm-lab`; токен в OpenBao `secret/gitea/agent`). Воркеры и песочницы работают от его имени, а не с админ-токеном, поэтому агент не может поменять защиту ветки или собственные гейты. На уже поднятом стенде запустите секцию один раз перед агентными флоу (m4a+).

## 4. Доступ к сервисам

| Сервис | Адрес | Доступ |
|---|---|---|
| Gitea Web/API | http://localhost:30080 | админ `swarm-admin` (пароль — в Vault) |
| Gitea SSH | localhost:30022 | git over ssh |
| OpenBao UI/API | port-forward 8200 | unseal/init ключи: `infra/.vault-unseal-keys.json`; root token: `infra/.vault-root-token` |
| Postgres | внутри кластера `postgres-platform-rw.platform:5432` | БД `gitea`, `openproject` (user `gitea`, secret `postgres-platform-app`) |
| OpenProject UI | http://localhost:30083 | `op-admin` (пароль в Vault `secret/openproject/admin`) ⚠ password-login сломан в v17-slim — см. TODO |
| LiteLLM API | http://localhost:30084/v1 | Bearer master key (Vault `secret/litellm/master`); модели: qwen-coder, qwen3, reviewer (z.ai Coding Plan; OpenRouter только при исчерпании лимита z.ai; ключи из `.env` в Vault `secret/litellm/{zai,openrouter}`) |
| Argo CD UI | http://localhost:30081 | `admin` / пароль из secret `argocd-initial-admin-secret` |
| Grafana | http://localhost:30082 | `admin` / пароль в Vault `secret/grafana/admin` |
| Prometheus | http://localhost:30090 | без авторизации (лаборатория); деплоер M6 берёт отсюда базовую линию |
| Панель + control plane | http://localhost:30400/ui | пользователь `owner`, пароль: `bash infra/scripts/platform.sh password`; служебным эндпоинтам нужен bearer-токен из Vault `secret/platform/api` (M11, TUTORIAL.ru.md §12) |
| isv-product (prod) | `kubectl -n prod get rollout isv-product` | канарейка Argo Rollouts; секции `rollouts` + `demo` (TUTORIAL.ru.md §8) |

## 5. Acceptance-тесты L0

```bash
./infra/tests/m2a_pr_flow.sh      # PR-flow с branch protection (нужен работающий bootstrap)
./infra/tests/m2b_deploy_flow.sh  # GitOps demo-deploy в prod + метрики + логи (нужен merge в main: Application следит за main)
./infra/tests/m2c_flow.sh         # задачи трекера → поллер + LLM-шлюз
./infra/tests/m2d_flow.sh         # CLI бутстрапера end to end
./infra/tests/m3_flow.sh          # control plane на живом Postgres
./infra/tests/m4a_flow.sh         # агент в docker → PR-ветка с evidence
./infra/tests/m4b_flow.sh         # агент в k3s-песочнице + NetworkPolicy
./infra/tests/m4c_flow.sh         # петля трекера через MCP + NetworkPolicy для MCP
./infra/tests/m5_flow.sh          # коридор гейтов: PR → CI → бот-ревью → HITL → мерж
```

На Windows-стенде добавляйте к каждому префикс `./infra/scripts/toolbox.sh bash` и запускайте **строго по одному**.

**Коридор гейтов (M5):**
- Воркер с `AGENT_RUNTIME_GATES_ENABLED=1` ждёт на HITL-гейте мержа. Ожидающие запросы показывает `curl $API/hitl/pending`.
- Подтвердить: `curl -X POST $API/hitl/<id>/decision -H 'Content-Type: application/json' -d '{"action":"approve","decided_by":"<you>"}'`. Отклонить — `"reject"`, PR при этом закрывается.
- `org-bootstrap onboard --project <name> [--stack python]` раскладывает check-профиль CI и политику мержа, выставляет `PLATFORM_GIT_URL` и защищает `main`.

### Запуск платформы (M11)

После bootstrap L0 (и `bootstrap.sh demo` для демо-сервиса): `bash infra/scripts/platform.sh up` из Git Bash на хосте. Команда пинит `origin/main` в worktree `.platform-live`, разворачивает control plane как приложение Argo CD `platform-control` и запускает контейнер-демон `swarm-platform-daemon`. Дальше заведите issue с меткой `swarm` в подключённом репо. Приёмка: `./infra/scripts/toolbox.sh bash infra/tests/m11_flow.sh`. Подробности — TUTORIAL.ru.md §12.

⚠ Rancher Desktop пробрасывает NodePort'ы на `0.0.0.0`, а брандмауэр Windows пропускает их входящими в профилях Private/Domain: стенд виден из вашей локальной сети (TUTORIAL.ru.md §12).

### Самообеспечение (спека §13.7, уровень 2)

Один раз завести этот репо в лабораторный Gitea (Git Bash на хосте; сначала `bootstrap.sh agent-user`): `bash infra/scripts/platform-sync.sh init` — создаёт `swarm-lab/ai-based-isv`, наполняет `main` из GitHub, применяет check-профиль **platform** и защищает `main` (push разрешён только `swarm-admin`). Далее на каждую задачу: `platform-sync.sh pull` → `./infra/scripts/toolbox.sh bash infra/tests/selfdev_flow.sh` (одобрить выведенный HITL-запрос, прочитав PR) → `platform-sync.sh promote` (открывает PR на GitHub) → мерж после GitHub CI → `platform-sync.sh pull`. Подробности — AGENTS.ru.md §12.

## 6. Troubleshooting

- **Порты 30080/30022/30081/30082 заняты** — освободить или поправить маппинги в `infra/k3d/config.yaml` и values Gitea/ArgoCD.
- **k3d-кластер в чужом состоянии** — `k3d cluster delete swarm-lab` и заново `./infra/bootstrap.sh` (данные лабораторные).
- **Vault после перезапуска кластера** — dev-mode эфемерен: бутстреппер пересоздаёт секреты; путь к постоянному бэкенду (raft) — M3.
- **Ресурсы Docker-VM** — полный стенд требует ≥6 CPU / 24 GB в Docker-VM (пример colima: `colima start --cpu 6 --memory 24`).
- **arm64** — все образы стенда multi-arch; при добавлении своего образа проверяйте наличие arm64-слоя.
