# INSTALL

Setup of the platform and its infrastructure (L0). Everything runs locally on a laptop (k3d = k3s in Docker); the manifests are identical for any k8s.

> Русская версия: [INSTALL.ru.md](INSTALL.ru.md)

## 1. Prerequisites

| Tool | Min version | Install |
|---|---|---|
| Docker | 28+ | Docker Desktop / OrbStack / colima (daemon must run) |
| Homebrew | any | [brew.sh](https://brew.sh) |
| k3d | 5.9+ | `brew install k3d` |
| helm | 4.x | `brew install helm` |
| kubectl | 1.37+ | `brew install kubectl` |
| uv | any recent | `brew install uv` |
| gh (optional) | 2.x | `brew install gh` |

Environment check:

```bash
docker info          # daemon responds
k3d version && helm version --short && kubectl version --client | head -1
```

Resources: ~12 GB RAM for the full L0 stand, ~20 GB disk. Ports `30080`, `30022`, `30081`, `30082` must be free.

## 2. Platform dev setup (the monorepo itself)

```bash
git clone https://github.com/kryadov/ai-based-isv && cd ai-based-isv
uv sync --all-packages      # venv + all workspace members
uv run ruff check .         # expected: All checks passed!
uv run mypy packages apps   # expected: no issues found
uv run pytest               # expected: all tests green
```

Python 3.12 is picked up automatically (`.python-version`).

## 3. L0 Bootstrap (k3d + Vault + Postgres + Gitea + GitOps + observability)

```bash
./infra/bootstrap.sh
```

Idempotent: a repeated run does not destroy data. The script creates the `swarm-lab` k3d cluster and provisions Vault (dev-mode), Postgres (CNPG, pgvector image), Gitea (NodePort `30080`, SSH `30022`, external DB), act_runner, Argo CD, kube-prometheus-stack (Grafana `30082`) and Loki+Alloy; the Gitea admin token is stored in Vault.

If the git repository is private, Argo CD is wired with `gh auth token` (requires `gh auth login` or `GITHUB_TOKEN`); for a public repo it clones anonymously.

Facts of the stand are recorded in `infra/env.yaml` (auto-generated — a VET artifact, do not edit by hand). Individual sections: `./infra/scripts/bootstrap.sh [vault|cnpg|gitea|runner|argocd|monitoring|loki|demo]`.

## 4. Service access

| Service | Address | Access |
|---|---|---|
| Gitea Web/API | http://localhost:30080 | admin `swarm-admin` (password — in Vault) |
| Gitea SSH | localhost:30022 | git over ssh |
| Vault | port-forward 8200 | root token: `kubectl -n platform exec vault-0 -- vault print token` |
| Postgres | in-cluster `postgres-platform-rw.platform:5432` | DBs `gitea`, `openproject` (user `gitea`, secret `postgres-platform-app`) |
| OpenProject UI | http://localhost:30083 | `op-admin` (password in Vault `secret/openproject/admin`) ⚠ password login broken in v17-slim — see TODO |
| LiteLLM API | http://localhost:30084/v1 | Bearer master key (Vault `secret/litellm/master`); models: qwen-coder, qwen3 |
| Argo CD UI | http://localhost:30081 | `admin` / password from secret `argocd-initial-admin-secret` |
| Grafana | http://localhost:30082 | `admin` / password in Vault `secret/grafana/admin` |

## 5. L0 acceptance tests

```bash
./infra/tests/m2a_pr_flow.sh      # PR-flow with branch protection (requires bootstrap)
./infra/tests/m2b_deploy_flow.sh  # GitOps demo-deploy to prod + metrics + logs (requires the demo merged to main: the Application follows main)
```

## 6. Troubleshooting

- **Ports 30080/30022/30081/30082 busy** — free them or adjust mappings in `infra/k3d/config.yaml` and Gitea/ArgoCD values.
- **k3d cluster in a foreign state** — `k3d cluster delete swarm-lab` and re-run `./infra/bootstrap.sh` (lab data).
- **Vault after a cluster restart** — dev-mode is ephemeral: the bootstrapper re-creates secrets; the persistent backend (raft) path is M3.
- **Docker VM resources** — the full stand needs ≥6 CPU / 24 GB in the Docker VM (colima example: `colima start --cpu 6 --memory 24`).
- **arm64** — all stand images are multi-arch; when adding your own image, verify an arm64 layer exists.
