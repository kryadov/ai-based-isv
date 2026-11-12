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
| psql (postgresql-client) | 14+ | `brew install libpq` / `apt install postgresql-client` (acceptance flows m3/m4a/m4b) |

Environment check:

```bash
docker info          # daemon responds
k3d version && helm version --short && kubectl version --client | head -1
```

Resources: ~12 GB RAM for the full L0 stand, ~20 GB disk. Ports `30080`, `30022`, `30081`, `30082` must be free.

**Windows (WSL2).** The bootstrapper and the acceptance flows are bash: run them inside a WSL2 distro (Ubuntu) against the Windows checkout (`/mnt/c/...`). Verified with Rancher Desktop (moby engine, WSL integration enabled for the distro); k3d, helm, gh, uv, psql installed inside the distro; Ollama runs natively on Windows (GPU) with `OLLAMA_HOST=0.0.0.0:11434` and is reached from the cluster as `host.docker.internal` (a one-line `ollama` shim forwarding to `ollama.exe` satisfies `org-bootstrap check`). Inside WSL use a separate uv environment (`UV_PROJECT_ENVIRONMENT=.venv-linux`) so the Windows `.venv` stays intact, and a stand-local `KUBECONFIG` (Rancher symlinks `~/.kube/config` to the Windows file). Line endings are forced to LF by `.gitattributes`.

## 2. Platform dev setup (the monorepo itself)

```bash
git clone https://github.com/kryadov/ai-based-isv && cd ai-based-isv
uv sync --all-packages      # venv + all workspace members
uv run ruff check .         # expected: All checks passed!
uv run mypy packages apps   # expected: no issues found
uv run pytest               # expected: all tests green
```

Python 3.12 is picked up automatically (`.python-version`).

## 3. L0 Bootstrap (k3d + OpenBao + Postgres + Gitea + GitOps + observability)

The primary path — the bootstrapper CLI:

```bash
uv sync --all-packages
uv run org-bootstrap check       # prerequisites
uv run org-bootstrap init --force  # questionnaire → infra/env.local.yaml (lab defaults)
uv run org-bootstrap apply       # idempotent provisioning of all L0 sections
uv run org-bootstrap seed-demo   # demo repo isv-product in Gitea
```

Idempotent: repeated runs do not destroy data. It creates the `swarm-lab` k3d cluster and provisions OpenBao (raft on PVC, auto-init/unseal), Postgres (CNPG, pgvector image), Gitea (NodePort `30080`, SSH `30022`, external DB), act_runner, Argo CD, kube-prometheus-stack (Grafana `30082`), Loki+Alloy; admin tokens/passwords are stored in OpenBao.

If the git repository is private, Argo CD is wired with `gh auth token` (requires `gh auth login` or `GITHUB_TOKEN`); for a public repo it clones anonymously.

Stand facts are recorded in `infra/env.yaml` (auto-generated — a VET artifact, do not edit by hand). Individual sections: `./infra/scripts/bootstrap.sh [vault|cnpg|gitea|runner|argocd|monitoring|loki|demo|openproject|litellm]` or `uv run org-bootstrap apply --section <name>`. Alternative without the CLI: `./infra/bootstrap.sh` (calls the same sections).

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
