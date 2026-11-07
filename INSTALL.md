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

Идемпотентен: повторный прогон не разрушает данные. Скрипт создаёт k3d-кластер `swarm-lab`, разворачивает Vault (dev-mode), Postgres (CNPG + pgvector), Gitea (NodePort `30080`, SSH `30022`) и act_runner; административный токен Gitea кладёт в Vault.

> Раздел финализируется в M2a Task 8 — после стабилизации фактических шагов.

## 4. Доступ к сервисам

| Сервис | Адрес | Доступ |
|---|---|---|
| Gitea Web/API | http://localhost:30080 | админ `swarm-admin` (пароль — в Vault) |
| Gitea SSH | localhost:30022 | git over ssh |
| Vault | port-forward 8200 | root token: `kubectl -n platform exec vault-0 -- vault print token` |
| Postgres | внутри кластера `postgres-platform-rw.platform:5432` | БД `gitea`, user `gitea` (secret `postgres-platform-credentials`) |

## 5. Acceptance-тесты L0

```bash
./infra/tests/m2a_pr_flow.sh   # PR-flow с branch protection
```

## 6. Troubleshooting

- **Порты 30080/30022 заняты** — освободить или поправить маппинги в `infra/k3d/config.yaml` и values Gitea.
- **k3d-кластер в чужом состоянии** — `k3d cluster delete swarm-lab` и заново `./infra/bootstrap.sh` (данные лабораторные).
- **Vault после перезапуска кластера** — dev-mode эфемерен: бутстреппер пересоздаёт секреты; путь к постоянному бэкенду (raft) — M3.
- **arm64** — все образы стенда multi-arch; при добавлении своего образа проверяйте наличие arm64-слоя.
