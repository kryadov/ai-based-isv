# M4b: Sandbox in k3s — pod manager, NetworkPolicy, Vault brokering — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The same executor flow as M4a, but the OpenCode harness runs in a k3s pod with a default-deny NetworkPolicy, a Vault-brokered read-only clone credential, PVC-backed workspace, and evidence unloaded from the pod — replacing the docker spawn without changing the `Executor` contract.

**Architecture:** New `agent_runtime/kube_sandbox.py` mirrors the `DockerServe` seam: a `KubeRunner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]` (argv, stdin) drives `kubectl` (no new Python deps; same injection-test pattern as M4a). Per vertex the worker: builds a Secret (opencode.jsonc + gitea token) and a Pod+NodePort-Service in the `agents` namespace, waits Ready, scrapes the serve password from pod logs, and hands `base_url=http://127.0.0.1:<nodeport>` + `auth` to the existing `OpencodeExecutor(base_url=...)` seam — the adapter code does not change. Teardown deletes Pod/Service/Secret; the PVC persists. Bootstrap gains an idempotent `section_agents` (namespace, default-deny NetworkPolicy, PVC, per-run allowance).

**Tech Stack:** Python ≥3.12, httpx, pydantic-settings; `kubectl` subprocess via runner seam; bash section in `infra/scripts/bootstrap.sh`; existing M4a modules (`opencode_executor.py`, `gitops.py`, `worker.py`, `done_when.py`, `pathguard.py`, `session_log.py`, `manifest.py`).

**Spec:** `docs/superpowers/specs/2026-11-09-m4-agent-runtime-design.md` (M4b section + "Решения builder check" items 5/9), ADR-0002, AGENTS.md §9.

## Global Constraints

- After every task green: `uv run ruff check .` · `uv run mypy packages apps` (zero suppressions) · `uv run pytest`.
- `packages/agent_runtime` stays free of FastAPI/SQLAlchemy and of the `kubernetes` python package — cluster access is `kubectl` subprocess only (controller ruling: reuse the M4a runner-seam pattern).
- English only in code/comments; explicit-path commits, no `git add -A` (AGENTS.md §9).
- No `kubectl port-forward` anywhere (STATE.md lesson — NodePort only).
- Sandbox pods NEVER receive git write credentials; the token travels into the initContainer via a per-run Secret, deleted at teardown.
- Do not recreate the k3d cluster without owner approval (STATE.md); cluster changes go through the idempotent `section_agents`.
- Live-stand work (spikes, acceptance) uses the established patterns: Gitea in-cluster `gitea-http.platform.svc:3000` (NodePort 30080 from host), LiteLLM NodePort 30084 via hostAliases → host IP (pattern of `section_litellm`), OpenBao read via `kubectl exec <openbao-pod> -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN=$(cat infra/.vault-root-token) bao kv get ...` (pattern of `infra/tests/m2a_pr_flow.sh:16`).

## Review Focus

1. **Orphaned sandbox pods after worker death** — a pod outliving its worker keeps serving with a live password and burns the NodePort. Expected: pods carry `agent-runtime.io/run-id`/`vertex-id` labels; worker startup reconciles (deletes labeled pods not matching a live lease — v0: deletes ALL labeled pods older than `lease_ttl_seconds*2` at startup). Test: Task 3 (fake-runner reconciliation).
2. **Secret (token) outliving the pod** — the per-run Secret with the Gitea token must be deleted on every teardown path, including pod-start failures. Expected: teardown is a single idempotent function covering Secret+Pod+Service. Test: Task 3.
3. **NodePort exhaustion/collision** — two concurrent vertices must not pick the same port. Expected: allocation queries existing NodePorts and picks the lowest free port from the configured range, retries on create-conflict. Test: Task 3.
4. **Evidence unload failure silently completing a vertex** — session log/manifest live on the PVC; if `kubectl cp` fails the evidence is gone and `complete` would be a false positive. Expected: unload failure → vertex `fail` (fail-closed), never complete. Test: Task 5.
5. **Default-deny NetworkPolicy breaking DNS or the harness** — if the allow-list misses kube-dns or the LiteLLM host IP, every pod hangs at startup with opaque timeouts. Expected: Task 1 spike verifies DNS + gitea + LiteLLM + blocked-Postgres BEFORE Task 3 hardens manifests; the negative assertion (Postgres unreachable) is pinned in Task 6's acceptance.

---

### Task 1: Spike — NetworkPolicy enforcement, image import, pod reachability (throwaway)

**Files:**
- Create: `docs/superpowers/research/2026-11-10-m4b-k3s-sandbox-spike.md` (research note only; committed)

**Interfaces:**
- Produces: confirmed facts for Tasks 2/3/6 — (a) does k3d/k3s enforce NetworkPolicy with the current cluster config (if NOT: options for the owner, do NOT recreate the cluster); (b) `k3d image import opencode-git:2.2.0 -c swarm-lab` works and a pod can pull it (`imagePullPolicy: IfNotPresent`); (c) in-cluster reachability matrix from a test pod in the `agents` namespace: gitea-http.platform.svc:3000, LiteLLM via hostAliases host-IP:30084, kube-dns; (d) Postgres (platform NodePort 15432) BLOCKED under the draft policy; (e) exact pod spec snippet that worked (volumes, hostAliases, probes).

- [ ] **Step 1:** Apply a draft netpol + test pod (alpine/git) in a scratch namespace `agents-spike`; run the reachability matrix with `wget`/`nc`; record stdout verdicts.
- [ ] **Step 2:** Import the image (build it first via the M4a docker path if absent: `docker build` with the Dockerfile from `_dockerfile()`), schedule a pod with it, confirm `opencode serve --hostname 0.0.0.0` boots and the password line appears in `kubectl logs`.
- [ ] **Step 3:** Write the note (verdicts a–e + gotchas); commit only the note: `git add docs/superpowers/research/2026-11-10-m4b-k3s-sandbox-spike.md && git commit -m "docs(m4b): k3s sandbox spike — netpol enforcement, image import, reachability"`. Delete the scratch namespace and spike pods.

### Task 2: `section_agents` — namespace, PVC, default-deny policy (bootstrap)

**Files:**
- Modify: `infra/scripts/bootstrap.sh` (add `section_agents()` following `section_cnpg` structure)
- Modify: `apps/bootstrap/src/bootstrap/cli.py:127` (section choice help text lists `agents`)
- Create: `infra/agents/namespace.yaml`, `infra/agents/pvc.yaml`, `infra/agents/netpol-default-deny.yaml`

**Interfaces:**
- Produces: namespace `agents`; PVC `agent-scratch` (ReadWriteOnce, 5Gi, default storageclass); NetworkPolicy `default-deny-all` (podSelector: {} — both ingress and egress denied). `section_agents` idempotent (`kubectl apply -f`), registered in `all`.

- [ ] **Step 1:** Write the three manifests (envsubst-free, plain YAML; labels `app.kubernetes.io/part-of: agent-runtime`).
- [ ] **Step 2:** Implement `section_agents()` (apply all three; retry-wrapped like `section_litellm`); add `agents` to the section list/help.
- [ ] **Step 3:** Verify live: `uv run org-bootstrap apply --section agents` → namespace/PVC/NetPolicy exist (show `kubectl get` output); re-run → no changes (idempotency evidence).
- [ ] **Step 4:** Commit explicit paths: `git add infra/agents infra/scripts/bootstrap.sh apps/bootstrap/src/bootstrap/cli.py && git commit -m "feat(infra): agents namespace, scratch PVC, default-deny NetworkPolicy (section_agents)"`.

### Task 3: `kube_sandbox.py` — pod/service/secret lifecycle via kubectl seam

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/kube_sandbox.py`
- Test: `packages/agent_runtime/tests/test_kube_sandbox.py`

**Interfaces:**
- Consumes: `OPENCODE_IMAGE` (settings), spike facts (pod spec shape).
- Produces: `KubeRunner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]` (same 2-arg shape as `_DockerRunner`; default `_kubectl_runner` = `subprocess.run(argv, capture_output=True, text=True, check=False, input=input)`); `class KubeSandbox` — `__init__(*, namespace: str = "agents", nodeport_range: tuple[int, int] = (30100, 30110), kube_runner: KubeRunner | None = None, image: str = OPENCODE_IMAGE, gitea_token: str, repo_url: str, base_branch: str, run_id: int, vertex_id: int, litellm_base_url: str, litellm_api_key: str, model: str)`; `start(timeout_seconds: float = 300.0) -> SandboxHandle`; `stop() -> None` (idempotent; deletes Service+Pod+Secret, keeps PVC). `SandboxHandle(BaseModel)`: `base_url: str` (credential-free, `http://127.0.0.1:<nodeport>`), `password: str`, `evidence_pod_path: str` (`/workspace/run-<id>/vertex-<id>`), `pod_name: str`. Object builders as module functions (testable pure): `secret_manifest(...) -> dict`, `pod_manifest(...) -> dict`, `service_manifest(...) -> dict` — pod: initContainer `alpine/git` (clone via `gitea-http.platform.svc:3000` with token env; writes `opencode.jsonc` from the Secret volume into the run dir), main container `opencode-git:<pin>` running `serve --hostname 0.0.0.0 --port 4096`, `OPENCODE_DB=/workspace/run-<id>/.opencode-db`, LLM endpoint in `opencode.jsonc` per the spike verdict (in-cluster `litellm` service DNS if reachable — preferred; hostAliases→host IP fallback), labels `agent-runtime.io/run-id`/`vertex-id`, PVC volume mount `/workspace`. Also `reconcile_orphans(kube_runner, namespace, max_age_seconds: float) -> int` (deletes labeled pods older than max_age; returns count).
- Behavior: `start()` allocates NodePort (query existing svc NodePorts, pick lowest free in range; on create-conflict retry next port), applies Secret→Pod→Service, waits pod Ready (poll `kubectl get pod -o json` via runner, generous timeout), scrapes password from `kubectl logs` (line `server password <token>`), returns handle. Non-zero kubectl → `KubeSandboxError(RuntimeError)` with stderr (no token echo — Secret data is base64 in manifest files written via stdin, not argv).

- [ ] **Step 1: Failing tests** (fake runner records argv/returns canned JSON): `test_pod_manifest_shape` (initContainer clones from in-cluster gitea URL; token only in Secret volume/env of initContainer; main container has no token; labels present; OPENCODE_DB on PVC path), `test_start_happy_path_returns_handle` (canned get→watch→logs sequence), `test_nodeport_allocation_skips_used` (two existing ports → picks third), `test_nodeport_conflict_retries_next`, `test_stop_deletes_service_pod_secret_idempotent`, `test_start_failure_cleans_up_secret_and_pod` (review focus 2), `test_reconcile_orphans_deletes_old_labeled_pods` (review focus 1), `test_runner_error_raises_kube_sandbox_error_without_token_echo`.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement per contracts. **Step 4:** Run → PASS; full gates green. **Step 5:** Commit: `git add packages/agent_runtime/src/agent_runtime/kube_sandbox.py packages/agent_runtime/tests/test_kube_sandbox.py && git commit -m "feat(agent_runtime): k3s sandbox lifecycle over kubectl seam"`.

### Task 4: Vault brokering — read-only Gitea token from OpenBao

**Files:**
- Create: `packages/agent_runtime/src/agent_runtime/vault_broker.py`
- Modify: `packages/agent_runtime/src/agent_runtime/settings.py` (add `vault_root_token_path: Path = Path("infra/.vault-root-token")`, `openbao_pod_label: str = "app.kubernetes.io/name=openbao"`, `openbao_namespace: str = "platform"`)
- Test: `packages/agent_runtime/tests/test_vault_broker.py`

**Interfaces:**
- Consumes: m2a exec pattern (constraint above).
- Produces: `read_gitea_token(kubectl_runner: KubeRunner, *, root_token: str, namespace: str = "platform", pod_label: str = "app.kubernetes.io/name=openbao") -> str` — resolves the openbao pod by label (`kubectl get pod -l ... -o json`), then `kubectl exec ... -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN=<root> bao kv get -field=token secret/gitea/admin`; returns the token; never logs it. NOTE: this is the ADMIN token v0 (per-project tokens are the M4c/M5 hardening line — recorded in the design's builder-check item 8).

- [ ] **Step 1: Failing tests** (fake runner): `test_read_gitea_token_returns_field` (argv contains `bao kv get -field=token secret/gitea/admin`, token passed via exec env not argv), `test_token_not_in_exception_on_failure`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS + gates. **Step 5:** Commit: `git add packages/agent_runtime/src/agent_runtime/vault_broker.py packages/agent_runtime/src/agent_runtime/settings.py packages/agent_runtime/tests/test_vault_broker.py && git commit -m "feat(agent_runtime): vault-brokered read-only gitea token"`.

### Task 5: Worker integration — backend dispatch, evidence unload, orphan reconcile

**Files:**
- Modify: `packages/agent_runtime/src/agent_runtime/settings.py` (`WorkerSettings`: + `sandbox_backend: str = "docker"`, `k8s_namespace: str = "agents"`, `nodeport_range_low: int = 30100`, `nodeport_range_high: int = 30110`, `host_ip: str = ""` (LiteLLM hostAliases target; required when backend=k3s), `vault_root_token_path`)
- Modify: `packages/agent_runtime/src/agent_runtime/worker.py` (`main()` builds `OpencodeExecutor(base_url=handle.base_url, ...)` from `KubeSandbox` when `sandbox_backend == "k3s"` (docker otherwise — unchanged); startup calls `reconcile_orphans`; after `wait_done()` and BEFORE complete/fail decision, when backend=k3s: unload evidence `kubectl cp <pod>:<evidence_pod_path>/{session_log.ndjson,manifest.json}` into the local evidence dir — **unload failure → vertex fail (Review Focus 4)**; teardown `handle.stop()` in the existing `finally` alongside `session.stop()`)
- Test: `packages/agent_runtime/tests/test_worker_k3s.py`

**Interfaces:**
- Consumes: `KubeSandbox/SandboxHandle` (T3), `read_gitea_token` (T4), existing worker loop.
- Produces: backend dispatch pinned by tests; evidence unload function `unload_evidence(kube_runner, handle, local_dir: Path) -> None` (kubectl cp both files; missing file → `EvidenceUnloadError`).

- [ ] **Step 1: Failing tests** (fake runner + FakeExecutor): `test_k3s_backend_uses_sandbox_base_url` (executor received base_url of the handle, not docker spawn), `test_evidence_unload_copies_both_files`, `test_evidence_unload_failure_fails_vertex_not_completes`, `test_startup_reconciles_orphan_pods`, `test_teardown_stops_sandbox_on_all_paths`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS + full gates. **Step 5:** Commit: `git add packages/agent_runtime/src/agent_runtime/worker.py packages/agent_runtime/src/agent_runtime/settings.py packages/agent_runtime/tests/test_worker_k3s.py && git commit -m "feat(agent_runtime): k3s sandbox backend in the vertex worker"`.

### Task 6: Acceptance `m4b_flow.sh` + spike follow-up (snapshot intermittency)

**Files:**
- Create: `infra/tests/m4b_flow.sh`
- Modify: `ARCH.md` (§8 verification map line), `STATE.md` (at increment close — controller)
- Create: `docs/superpowers/research/2026-11-10-m4b-snapshot-intermittency.md` (the T10 carry-over spike, time-boxed)

**Interfaces:**
- Consumes: everything above; live stand (m2/m3/m4a green).

- [ ] **Step 1:** Write `m4b_flow.sh` in the style of `m4a_flow.sh`: preflight (stand, `section_agents` applied, image imported into k3d, tool_calls assertion) → seed repo → run+vertex (same vertex/done_when/paths_allow as m4a) → worker with `AGENT_RUNTIME_SANDBOX_BACKEND=k3s` → poll psql for done → assert: branch in Gitea with content; evidence files LOADED FROM THE POD exist locally with `harness_event`; manifest verdict.passed=true; pod/service/secret deleted after the vertex; **negative netpol test**: exec a wget from a fresh agents-namespace pod to Postgres NodePort 15432 → must FAIL (blocked).
- [ ] **Step 2:** Snapshot-intermittency spike (time-box 45 min): N=5 consecutive sessions against a fresh k3s sandbox clone; record empty-diff frequency and any correlation; one-paragraph verdict + recommendation in the research note (if reproducible → propose mitigation for M4c, do not fix here).
- [ ] **Step 3:** Run `./infra/tests/m4b_flow.sh` → PASS (attach output tail). Regressions: m2a–d, m3, m4a → PASS. Gates: ruff/mypy/pytest → clean.
- [ ] **Step 4:** Commit: `git add infra/tests/m4b_flow.sh ARCH.md docs/superpowers/research/2026-11-10-m4b-snapshot-intermittency.md && git commit -m "test(m4b): k3s sandbox acceptance flow + snapshot spike"`.

## Self-review notes

- Design M4b coverage: pod manager (T3), initContainer clone with read-creds only (T3), NetworkPolicy allow-list + negative test (T2+T6), Vault worker-brokering (T4), PVC (T2/T3), evidence unload (T5), same Executor contract (T5 uses the M4a base_url seam unchanged), snapshot spike (T6). M4c (MCP) untouched here.
- KubeSandbox error semantics mirror DockerServe; `OpencodeExecutor` code is NOT modified in M4b (only consumed via `base_url`) — that is the M4a→M4b continuity the design promised.
- Known deferred minors carried from M4a that M4b does NOT address: httpx/httpx2 consolidation (M5), per-project Gitea tokens (M4c/M5), USER-root pod-ownership policy (partially addressed here: pods run the image's user; PVC ownership policy → M4c if evidence shows problems).
