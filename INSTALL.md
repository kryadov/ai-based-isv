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

**Windows (Rancher Desktop + toolbox) — the current stand (since 2026-11-14).** Use Rancher Desktop's BUILT-IN Kubernetes (k3s, moby engine) as the stand cluster — no k3d, no separate WSL distro. All bash/Python tooling runs in the toolbox container: `./infra/scripts/toolbox.sh <cmd>` from Git Bash at the repo root (e.g. `./infra/scripts/toolbox.sh uv run pytest`, `./infra/scripts/toolbox.sh bash infra/tests/m4c_flow.sh`, no args = shell). The toolbox (`infra/toolbox/Dockerfile`: uv, kubectl, helm, gh, psql, docker CLI) runs with host networking and the host docker socket, mounts the repo at the same `/mnt/c/...` path the daemon sees, and sets `SWARM_CLUSTER=rancher-desktop` — the bootstrapper then skips `k3d image import` (k3s shares the moby image store) and `k3d --port-add` (Rancher forwards every NodePort to localhost). Native Python on Windows is not an option on hosts with Smart App Control (it blocks uv's CPython, wheel DLLs and uv's `.exe` launchers). `toolbox.sh` also installs `infra/rancher/swarm-lab.start` into `%LOCALAPPDATA%\rancher-desktop\provisioning\` (k3s `service-node-port-range: 10000-32767` for Postgres 15432 + inotify limits 1024/1048576); restart Rancher Desktop once after it is installed. Disable Traefik (`rdctl set --kubernetes.options.traefik=false`). LLM: external APIs (the stand has no GPU, no local inference) — the z.ai GLM Coding Plan for everything; OpenRouter ONLY once the z.ai plan limit is exhausted (HTTP 429 → z.ai parked 5 min; other z.ai errors never reroute — `infra/litellm/zai_limit_gate.py`). `cp .env.example .env`, fill `ZAI_API_KEY` and/or `OPENROUTER_API_KEY`; every `litellm` section run writes them into OpenBao (`secret/litellm/{zai,openrouter}`) and syncs them into the Secret `litellm-provider` (key rotation = edit `.env` + re-run). `.env` is gitignored — never commit keys.

**Windows (WSL2, previous stand).** The bootstrapper and the acceptance flows are bash: run them inside a WSL2 distro (Ubuntu) against the Windows checkout (`/mnt/c/...`). Verified with Rancher Desktop (moby engine, WSL integration enabled for the distro); k3d, helm, gh, uv, psql installed inside the distro; Ollama runs natively on Windows (GPU) with `OLLAMA_HOST=0.0.0.0:11434` and is reached from the cluster as `host.docker.internal` (a one-line `ollama` shim forwarding to `ollama.exe` satisfies `org-bootstrap check`). Inside WSL use a separate uv environment (`UV_PROJECT_ENVIRONMENT=.venv-linux`) so the Windows `.venv` stays intact, and a stand-local `KUBECONFIG` (Rancher symlinks `~/.kube/config` to the Windows file). Line endings are forced to LF by `.gitattributes`. Two host settings are mandatory: (1) raise the shared WSL2 kernel inotify limit — `%UserProfile%\.wslconfig` → `[wsl2]` `kernelCommandLine = sysctl.fs.inotify.max_user_instances=1024 sysctl.fs.inotify.max_user_watches=1048576` (the default 128 instances is exhausted by the cluster and the sandbox opencode server hangs silently); (2) set `OLLAMA_CONTEXT_LENGTH=16384` — Ollama derives a 4k default context from VRAM below 24 GB and silently truncates the ~8k-token harness prompt, so the agent loses its instructions.

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

Stand facts are recorded in `infra/env.yaml` (auto-generated — a VET artifact, do not edit by hand). Individual sections: `./infra/scripts/bootstrap.sh [vault|cnpg|gitea|agent-user|runner|argocd|monitoring|loki|demo|openproject|litellm]` or `uv run org-bootstrap apply --section <name>`. Alternative without the CLI: `./infra/bootstrap.sh` (calls the same sections). **`agent-user`** (runs right after `gitea` in `all`) creates the NON-admin Gitea user `swarm-agent` (team `agents`, write on every `swarm-lab` repo; token in OpenBao `secret/gitea/agent`). Workers and sandboxes act as this user — never with the admin token — so an agent cannot change branch protection or its own gates. On an existing stand run it once before the agent flows (m4a+).

## 4. Service access

| Service | Address | Access |
|---|---|---|
| Gitea Web/API | http://localhost:30080 | admin `swarm-admin` (password — in Vault) |
| Gitea SSH | localhost:30022 | git over ssh |
| Vault | port-forward 8200 | root token: `kubectl -n platform exec vault-0 -- vault print token` |
| Postgres | in-cluster `postgres-platform-rw.platform:5432` | DBs `gitea`, `openproject` (user `gitea`, secret `postgres-platform-app`) |
| OpenProject UI | http://localhost:30083 | `op-admin` (password in Vault `secret/openproject/admin`) ⚠ password login broken in v17-slim — see TODO |
| LiteLLM API | http://localhost:30084/v1 | Bearer master key (Vault `secret/litellm/master`); models: qwen-coder, qwen3, reviewer (z.ai Coding Plan; OpenRouter only on the z.ai plan limit; keys from `.env` into Vault `secret/litellm/{zai,openrouter}`) |
| Argo CD UI | http://localhost:30081 | `admin` / password from secret `argocd-initial-admin-secret` |
| Grafana | http://localhost:30082 | `admin` / password in Vault `secret/grafana/admin` |

## 5. L0 acceptance tests

```bash
./infra/tests/m2a_pr_flow.sh      # PR-flow with branch protection (requires bootstrap)
./infra/tests/m2b_deploy_flow.sh  # GitOps demo-deploy to prod + metrics + logs (requires the demo merged to main: the Application follows main)
./infra/tests/m2c_flow.sh         # tracker issues → poller + LLM gateway
./infra/tests/m2d_flow.sh         # bootstrap CLI end to end
./infra/tests/m3_flow.sh          # control plane on live Postgres
./infra/tests/m4a_flow.sh         # agent in docker → PR branch with evidence
./infra/tests/m4b_flow.sh         # agent in a k3s sandbox pod + NetworkPolicy
./infra/tests/m4c_flow.sh         # tracker loop via MCP + MCP NetworkPolicy
./infra/tests/m5_flow.sh          # gate corridor: PR → CI → bot review → HITL → merged
```

On the Windows stand, prefix each with `./infra/scripts/toolbox.sh bash`, and run them **one at a time**.

**Gate corridor (M5):**
- A worker with `AGENT_RUNTIME_GATES_ENABLED=1` waits at the HITL merge gate. List the pending requests with `curl $API/hitl/pending`.
- Approve: `curl -X POST $API/hitl/<id>/decision -H 'Content-Type: application/json' -d '{"action":"approve","decided_by":"<you>"}'`. To reject, use `"reject"`, which closes the PR.
- `org-bootstrap onboard --project <name> [--stack python]` seeds the CI check profile and merge policy, sets `PLATFORM_GIT_URL` and protects `main`.

## 6. Troubleshooting

- **Ports 30080/30022/30081/30082 busy** — free them or adjust mappings in `infra/k3d/config.yaml` and Gitea/ArgoCD values.
- **k3d cluster in a foreign state** — `k3d cluster delete swarm-lab` and re-run `./infra/bootstrap.sh` (lab data).
- **Vault after a cluster restart** — dev-mode is ephemeral: the bootstrapper re-creates secrets; the persistent backend (raft) path is M3.
- **Docker VM resources** — the full stand needs ≥6 CPU / 24 GB in the Docker VM (colima example: `colima start --cpu 6 --memory 24`).
- **arm64** — all stand images are multi-arch; when adding your own image, verify an arm64 layer exists.
