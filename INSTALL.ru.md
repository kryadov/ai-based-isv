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

**Windows (WSL2).** Бутстрапер и acceptance-флоу — bash: запускайте их внутри WSL2-дистрибутива (Ubuntu) по Windows-чекауту (`/mnt/c/...`). Проверено с Rancher Desktop (движок moby, WSL-интеграция включена для дистрибутива); k3d, helm, gh, uv, psql — внутри дистрибутива; Ollama — нативно на Windows (GPU) с `OLLAMA_HOST=0.0.0.0:11434`, из кластера доступна как `host.docker.internal` (однострочный шим `ollama` → `ollama.exe` закрывает `org-bootstrap check`). В WSL используйте отдельное uv-окружение (`UV_PROJECT_ENVIRONMENT=.venv-linux`), чтобы не затирать Windows-`.venv`, и локальный для стенда `KUBECONFIG` (Rancher симлинкает `~/.kube/config` на Windows-файл). Окончания строк принудительно LF через `.gitattributes`. Две настройки хоста обязательны: (1) поднять общий для ядра WSL2 лимит inotify — `%UserProfile%\.wslconfig` → `[wsl2]` `kernelCommandLine = sysctl.fs.inotify.max_user_instances=1024 sysctl.fs.inotify.max_user_watches=1048576` (дефолтные 128 инстансов выедает кластер, и opencode-сервер в песочнице молча виснет); (2) задать `OLLAMA_CONTEXT_LENGTH=16384` — при VRAM меньше 24 GB Ollama берёт контекст 4k по умолчанию и молча обрезает ~8k-токенный промпт harness, агент теряет инструкции.

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

Факты стенда фиксируются в `infra/env.yaml` (генерируется автоматически — артефакт VET, руками не редактировать). Секции можно гонять по отдельности: `./infra/scripts/bootstrap.sh [vault|cnpg|gitea|runner|argocd|monitoring|loki|demo]`.

## 4. Доступ к сервисам

| Сервис | Адрес | Доступ |
|---|---|---|
| Gitea Web/API | http://localhost:30080 | админ `swarm-admin` (пароль — в Vault) |
| Gitea SSH | localhost:30022 | git over ssh |
| OpenBao UI/API | port-forward 8200 | unseal/init ключи: `infra/.vault-unseal-keys.json`; root token: `infra/.vault-root-token` |
| Postgres | внутри кластера `postgres-platform-rw.platform:5432` | БД `gitea`, `openproject` (user `gitea`, secret `postgres-platform-app`) |
| OpenProject UI | http://localhost:30083 | `op-admin` (пароль в Vault `secret/openproject/admin`) ⚠ password-login сломан в v17-slim — см. TODO |
| LiteLLM API | http://localhost:30084/v1 | Bearer master key (Vault `secret/litellm/master`); модели: qwen-coder, qwen3 |
| Argo CD UI | http://localhost:30081 | `admin` / пароль из secret `argocd-initial-admin-secret` |
| Grafana | http://localhost:30082 | `admin` / пароль в Vault `secret/grafana/admin` |

## 5. Acceptance-тесты L0

```bash
./infra/tests/m2a_pr_flow.sh      # PR-flow с branch protection (нужен работающий bootstrap)
./infra/tests/m2b_deploy_flow.sh  # GitOps demo-deploy в prod + метрики + логи (нужен merge в main: Application следит за main)
```

## 6. Troubleshooting

- **Порты 30080/30022/30081/30082 заняты** — освободить или поправить маппинги в `infra/k3d/config.yaml` и values Gitea/ArgoCD.
- **k3d-кластер в чужом состоянии** — `k3d cluster delete swarm-lab` и заново `./infra/bootstrap.sh` (данные лабораторные).
- **Vault после перезапуска кластера** — dev-mode эфемерен: бутстреппер пересоздаёт секреты; путь к постоянному бэкенду (raft) — M3.
- **Ресурсы Docker-VM** — полный стенд требует ≥6 CPU / 24 GB в Docker-VM (пример colima: `colima start --cpu 6 --memory 24`).
- **arm64** — все образы стенда multi-arch; при добавлении своего образа проверяйте наличие arm64-слоя.
