# INSTALL

Установка платформы и её инфраструктуры (L0). Всё разворачивается локально на ноутбуке (k3d = k3s в Docker); манифесты идентичны для любого k8s.

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

Проверка окружения:

```bash
docker info          # демон отвечает
k3d version && helm version --short && kubectl version --client | head -1
```

Ресурсы: ~12 GB RAM под весь L0-стенд, ~20 GB диска. Порты `30080`, `30022` должны быть свободны (Gitea HTTP/SSH).

## 2. Platform dev setup (сам монорепо)

```bash
git clone https://github.com/kryadov/ai-based-isv && cd ai-based-isv
uv sync --all-packages      # venv + все члены workspace
uv run ruff check .         # ожидание: All checks passed!
uv run mypy packages apps   # ожидание: no issues found
uv run pytest               # ожидание: все тесты зелёные
```

Python 3.12 подхватится автоматически (`.python-version`).

## 3. L0 Bootstrap (k3d + Vault + Postgres + Gitea)

```bash
./infra/bootstrap.sh
```

Идемпотентен: повторный прогон не разрушает данные. Скрипт создаёт k3d-кластер `swarm-lab`, разворачивает Vault (dev-mode), Postgres (CNPG, образ с pgvector), Gitea (NodePort `30080`, SSH `30022`, внешняя БД) и act_runner; административный токен Gitea кладёт в Vault (`secret/gitea/admin`).

Факты стенда фиксируются в `infra/env.yaml` (генерируется автоматически — артефакт VET, руками не редактировать). Секции можно гонять по отдельности: `./infra/scripts/bootstrap.sh [vault|cnpg|gitea|runner]`.

## 4. Доступ к сервисам

| Сервис | Адрес | Доступ |
|---|---|---|
| Gitea Web/API | http://localhost:30080 | админ `swarm-admin` (пароль — в Vault) |
| Gitea SSH | localhost:30022 | git over ssh |
| Vault | port-forward 8200 | root token: `kubectl -n platform exec vault-0 -- vault print token` |
| Postgres | внутри кластера `postgres-platform-rw.platform:5432` | БД `gitea`, user `gitea` (secret `postgres-platform-app`) |
| Argo CD UI | http://localhost:30081 | `admin` / пароль из secret `argocd-initial-admin-secret` |
| Grafana | http://localhost:30082 | `admin` / пароль в Vault `secret/grafana/admin` |

## 5. Acceptance-тесты L0

```bash
./infra/tests/m2a_pr_flow.sh      # PR-flow с branch protection (нужен работающий bootstrap)
./infra/tests/m2b_deploy_flow.sh  # GitOps demo-deploy в prod + метрики + логи (нужен merge в main: Application следит за main)
```

Примечание: m2b-тест выполняется после `./infra/scripts/bootstrap.sh demo` на состоянии main (Application demo-prod синкается из GitHub main).

## 6. Troubleshooting

- **Порты 30080/30022 заняты** — освободить или поправить маппинги в `infra/k3d/config.yaml` и values Gitea.
- **k3d-кластер в чужом состоянии** — `k3d cluster delete swarm-lab` и заново `./infra/bootstrap.sh` (данные лабораторные).
- **Vault после перезапуска кластера** — dev-mode эфемерен: бутстреппер пересоздаёт секреты; путь к постоянному бэкенду (raft) — M3.
- **Argo CD и приватный GitHub-репо** — если репо private, бутстреппер подключает токен `gh auth token` (нужен `gh auth login` или `GITHUB_TOKEN`); для public-репо клонирует анонимно.
- **arm64** — все образы стенда multi-arch; при добавлении своего образа проверяйте наличие arm64-слоя.
