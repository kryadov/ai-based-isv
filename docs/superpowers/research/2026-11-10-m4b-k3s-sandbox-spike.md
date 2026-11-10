# M4b spike — k3s sandbox: NetworkPolicy enforcement, image import, reachability

Date: 2026-11-10 · Throwaway probes on the live k3d `swarm-lab` stand; scratch namespace `agents-spike` deleted after probing.

## Verdicts

**(a) NetworkPolicy IS enforced on this k3d cluster.** A default-deny (Ingress+Egress, `podSelector: {}`) plus an allow-list behaved exactly as specified — no cluster recreation needed.

Verified matrix (alpine probe pod in `agents-spike` under the draft policy):

| Target | Result | Evidence |
|---|---|---|
| kube-dns | ALLOWED | `nslookup gitea-http.platform.svc.cluster.local` → 10.43.118.226 |
| `gitea-http.platform.svc:3000` | ALLOWED | `GET /api/v1/version` → `{"version":"1.27.0"}` |
| `litellm.platform.svc:4000` | ALLOWED | `GET /health/liveliness` → `"I'm alive!"` |
| `postgres-platform-rw.platform.svc:5432` | BLOCKED | `nc -zv` → exit=1 |
| host IP `192.168.5.2:15432` (Postgres NodePort) | BLOCKED | `nc -zv` → exit=1 |

Policy shape that worked (draft for Task 2/3):

```yaml
egress:
  - to: {namespaceSelector: kube-system}      # DNS 53 UDP+TCP
  - to: {namespaceSelector: platform}         # TCP 3000 (gitea-http), TCP 4000 (litellm)
ingress:
  - ports: [{protocol: TCP, port: 4096}]      # serve, unrestricted source (laptop lab)
```

**(b) `k3d image import opencode-git:2.2.0 -c swarm-lab` works** (`Successfully imported 1 image(s)`); a pod with `imagePullPolicy: IfNotPresent` schedules and boots it.

**(c) In-cluster services are the right endpoints — hostAliases NOT needed.** LiteLLM is reachable in-cluster at `litellm.platform.svc:4000` (its own Service; the NodePort 30084 + hostAliases trick was only ever needed for the DOCKER path reaching the host). Gitea likewise in-cluster at `gitea-http.platform.svc:3000`. **In-pod git clone verified**: `git clone http://oauth2:<token>@gitea-http.platform.svc.cluster.local:3000/...` succeeds from an `alpine/git` pod (scheme must be `http://` — port 3000 is plain HTTP; `https://` fails with `TLS connect error: wrong version number`).

**(d) Postgres is blocked under the draft policy** — both the cluster service (:5432) and the host NodePort (:15432) (see matrix).

**(e) Working pod/service spec + the NodePort publishing gotcha:**

- Main container: `image: opencode-git:2.2.0`, `imagePullPolicy: IfNotPresent`, `command: ["opencode","serve","--hostname","0.0.0.0","--port","4096"]`, `workingDir: /workspace`. The log line `server password <token>` appears in `kubectl logs` (same format as M4a; 43-char token observed).
- `NodePort` Service + host curl verified end-to-end: bad creds → `401`, real pod creds → `POST /api/session` with `{}` → `2xx` + session JSON (the M4a bodyless-session fix carries over).
- **GOTCHA — k3d publishes only individually mapped ports.** A NEW NodePort (30100) is accepted by k8s but unreachable from the host (`connection refused`) until the load balancer publishes it. Fixed live with `k3d cluster edit swarm-lab --port-add "30100-30110:30100-30110@loadbalancer"` — k3d swaps the serverlb container in place (~16 s), no cluster recreation, existing mappings preserved. `section_agents` (Task 2) must do this guarded: check `docker port k3d-swarm-lab-serverlb` for the range first, add only if missing.

## Operational findings (carry into STATE/tasks)

1. **OpenBao was SEALED on the stand** (colima/host restart seals it; bootstrap auto-unseals only during `apply`). Manual unseal: `kubectl -n platform exec <openbao-pod> -- env BAO_ADDR=http://127.0.0.1:8200 bao operator unseal <key0>` with `infra/.vault-unseal-keys.json` (keys file was missing in the worktree — copied from the main checkout). Any acceptance flow that reads secrets must preflight unseal status.
2. The clone probe used the token inline in the URL (visible in the pod spec). For Task 3 the token must reach the initContainer as an env var from a per-run Secret (pod spec then carries no secret material in argv/args).
3. One-shot pods (`restartPolicy: Never`) that exit never become `Ready` — waiting on Ready is for the SERVE pod only; clone validation belongs to initContainer semantics (runs to completion, then main starts).
