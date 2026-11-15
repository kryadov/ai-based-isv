# M6: canary rollout + post-rollout metric analysis

Spec §7.2.6 and §8 (M6), plus the TODO item "Post-rollout metric regression monitoring", which is mandatory. The owner asked to continue with M6 under the standing directive: work without stopping, ask only critical questions. Every "Ruling" below is my call, open to correction.

## Intent

The corridor currently ends at the merge. M6 extends it to production, so that the spec's output ("a change rolled out to production with a canary deploy") becomes real for a service project:

**merge → image build → Argo Rollouts canary (20 → 50 → 100%) with metric analysis against the stable version → post-rollout analysis window → a success marker in evidence, OR automatic rollback with a postmortem draft**

No rollout counts as successful without a positive metrics verdict.

**Acceptance (`m6_flow.sh`):**
1. **Good path:** an agent task on `isv-product` is merged through the corridor, then deployed as a canary. The analysis passes, the Rollout is Healthy on the new image, and the evidence carries the marker.
2. **Bad path:** a build with injected faults is deployed the same way. The analysis fails, Argo Rollouts aborts and rolls back to the stable version, and the evidence carries the failed verdict and a postmortem draft.

## Constraints

- **Tooling the spec fixes:** k3s + Argo CD + Argo Rollouts; metrics from kube-prometheus-stack (already on the stand, with `serviceMonitorSelectorNilUsesHelmValues: false`, so every ServiceMonitor is scraped); synthetic traffic from k6 next to the service.
- **"Prod" is namespace `prod`**, behind the Argo CD AppProject `swarm-lab` (spec §14).
- **HITL gate #3** (spec §7.2.6): the first prod rollout of a new service and any change to deploy config require a human.
- **Agents stay out of deploy:** they get read-only deploy/observability MCP tools, and deploy actions are platform-side.
- **One stand:** no image registry beyond what k3s can see. On Rancher Desktop, k3s shares the moby image store, so a `docker build` on the host is directly runnable. k3d needs `k3d image import`, which is already handled for the agent images.
- **LLM traffic:** z.ai only until its limit (unchanged).

## Approaches

1. **Argo Rollouts canary, Prometheus AnalysisTemplates, deployer in the platform worker (chosen).**
   - After the gates merge, the worker's deployer builds the image from the merged commit and points the Argo CD Application at it (kustomize image override on the Application).
   - Argo CD syncs, and Argo Rollouts runs the steps with analysis.
   - The deployer watches the Rollout to a terminal phase and writes the verdict.
2. **GitOps tag commit:** CI builds and pushes, then commits the new tag to `deploy/` on `main`. Rejected for v0: CI would need push rights on protected `main`, or a bot PR per deploy that itself needs the corridor. That is a loop with HITL on every deploy, plus a registry.
3. **Flagger or a custom controller.** Rejected: the spec chose Argo Rollouts, and a custom controller re-implements it.

## Design

### 1. `isv_product` becomes a real service (`apps/isv_product`)

- **Endpoints:** `GET /healthz`, `GET /api/greet?name=` (business endpoint) and `GET /metrics` (Prometheus via `prometheus-client`).
- **Metrics:** `http_requests_total{route,code}` and `http_request_duration_seconds` (histogram, `route`).
- **Fault injection** for the bad path, read at startup:
  - `FAULT_ERROR_RATE` (0..1): returns 500 on `/api/*` with that probability;
  - `FAULT_LATENCY_MS`: adds a delay.

  Defaults are 0. Ruling: env-driven faults, so the bad path needs no bad code: the same image with different env. The deployer's `--fault` option sets them on the Rollout pod template. Using it is only allowed in the acceptance flow, and it is recorded in evidence.
- **Packaging:** a `Dockerfile` (python:3.12-slim, uvicorn), plus tests.

### 2. Deploy manifests: `infra/templates/deploy/service/`

These are seeded into the project repo as `deploy/` by `seed-demo` / `onboard --kind service`:

- **`rollout.yaml`:** an Argo `Rollout` (replicas 4, image `isv-product:seed`, readiness on `/healthz`) with canary steps:
  1. `setWeight 20` → pause 60s;
  2. `setWeight 50` → pause 60s;
  3. `setWeight 100` → `analysis post-rollout`.

  During the canary steps a background analysis runs `canary-vs-stable`.

  Ruling: replica-based weights (no service mesh or ingress on the stand); 4 replicas give 25/50/100 granularity, and Argo rounds the weights.
- **`service.yaml`:** a stable Service plus a ServiceMonitor with `podTargetLabels: [rollouts-pod-template-hash]`, so the metrics carry the version.
- **`analysis.yaml`:** two AnalysisTemplates against `http://kube-prometheus-stack-prometheus.platform:9090`:
  - **`canary-vs-stable`** (interval 30s, count 4, failureLimit 1):
    - the canary's 5xx ratio ≤ max(stable ratio × 2, 0.05);
    - the canary's p95 ≤ max(stable p95 × 1.5, 0.3 s);
    - both computed over `[1m]` by `rollouts-pod-template-hash`, with the stable and canary hashes passed as args.
  - **`post-rollout`** (interval 30s, count 4, failureLimit 1): after 100%, the service-wide 5xx ratio ≤ max(baseline × 2, 0.05) and p95 ≤ max(baseline × 1.5, 0.3 s), where the baseline is the pre-rollout window passed in by the deployer.

  Ruling: absolute floors keep a near-zero baseline from making the check flaky. A failure aborts the Rollout, and Argo Rollouts scales the stable ReplicaSet back (automatic rollback).
- **`k6.yaml`:** a Deployment running `grafana/k6` against the Service at a constant 10 rps, with a script in a ConfigMap covering `/api/greet` and `/healthz`.
- **`kustomization.yaml`:** the resources plus `images: [{name: isv-product}]`.

### 3. Stand sections

- **`bootstrap.sh rollouts`:** a Helm install of argo-rollouts with a pinned chart version into `platform`. The AppProject gets the Rollout and AnalysisTemplate resource kinds whitelisted, and Gitea is added as a `sourceRepos` entry (`http://gitea-http.platform.svc.cluster.local:3000/*`).
- **`section_demo`** (rewritten): the Argo CD Application `isv-product-prod` points at `swarm-lab/isv-product`, path `deploy`, kustomize, auto-sync and selfHeal. Argo CD reads Gitea with a read-only token: the agent user's token is reused, as it can read all `swarm-lab` repos (ruling). podinfo is removed.
- **`seed-demo`:** seeds `swarm-lab/isv-product` (org repo) with the app code, `deploy/`, the python check profile and protection. `deploy/**` joins the global `PROTECTED_GLOBS`. Ruling: global rather than per repo, since deploy-config changes are human-only everywhere (spec HITL #3) and a repo without `deploy/` is unaffected.

### 4. Deployer (`packages/agent_runtime/src/agent_runtime/deployer.py`)

`deploy(commit_sha, *, app, namespace, image_repo, fault=None) -> DeployVerdict`:

1. Clone at `commit_sha`, then `docker build -t isv-product:<sha7>` (on k3d also run `k3d image import`).
2. **Baseline:** query Prometheus for the current service-wide 5xx ratio and p95 over the last 5 minutes. With no traffic yet, use the absolute floors.
3. **First rollout** (no stable ReplicaSet) or a change under `deploy/**`: create a HITL request of kind `deploy` and wait (HITL #3).
4. Patch the Argo CD Application `spec.source.kustomize.images = ["isv-product=isv-product:<sha7>"]` (plus fault env through `kustomize.patches` when `fault` is set). Argo CD syncs.
5. Watch `rollout.status.phase` until Healthy or Degraded/aborted (timeout 15 min). Collect the AnalysisRun results.
6. Return the verdict: `{status: succeeded|rolled_back|timeout, image, revision, analysis: [...], baseline, started, finished}`.
   - It is appended to the session log (`deploy`) and to the manifest (`deploy_verdict`).
   - A tracker note: `[platform] deployed <sha7> to prod: succeeded` or `rolled back: <failed metric>`.
   - On rollback, `postmortem.md` (draft: timeline, failed metric values against thresholds, image, revision) goes into the evidence dir. The episodic memory write-back is M7.

The worker calls the deployer after the gates merge when `deploy_enabled`. The vertex completes with `deploy_verdict`; a rollback fails the vertex with `deploy:rolled_back`, which does not trigger a retry.

Ruling: a rollback is a verdict, not a transient error, so it is terminal for the vertex. This reuses the "terminal fail" need from the HITL-reject TODO: it is implemented here as a `terminal: true` flag on `/vertices/{id}/fail`, and the HITL reject adopts it too. That closes that TODO item.

Also a CLI, `uv run python -m agent_runtime.deployer --sha … [--fault error_rate=0.5]`, for the bad path and for operators.

### 5. MCP servers (read-only for agents)

- **`deploy`:** `get_rollout(app)` returns the phase, the current and stable images, the step and the last AnalysisRun.
- **`observability`:** `query_metric(promql)` returns an instant vector (query only, capped result size), and `service_slo(app)` returns the current 5xx ratio and p95.

Both are added to the existing `mcp-servers` image and NetworkPolicy (sandbox-access) like `ci`. The worker registers them in the harness config.

### 6. Acceptance: `infra/tests/m6_flow.sh`

1. Preflight: Argo CD, Argo Rollouts and Prometheus are up, `isv-product` is seeded, and the Application is Healthy on the seed image. The first rollout was approved at seeding, recorded as the HITL #3 decision by the operator.
2. **Good path:**
   - an agent task on `isv-product` ("add `GET /api/time`", with tests) → corridor → merge (the flow auto-approves HITL, as in m5);
   - the deployer runs the canary;
   - assert: verdict `succeeded`, Rollout Healthy on `<sha7>`, AnalysisRuns `Successful`, the marker in evidence, the tracker note.
3. **Bad path:**
   - the deployer CLI deploys the same commit with `--fault error_rate=0.5`;
   - assert: verdict `rolled_back`, the Rollout is back to the previous stable image, the `canary-vs-stable` AnalysisRun is `Failed`, and `postmortem.md` exists.

The flow then restores the Rollout to Healthy.

## Non-goals

- Staging environment promotion.
- Ingress or traffic-split by mesh.
- Multi-service rollouts.
- Episodic memory write-back (M7).
- Canary for the platform itself: platform releases stay GitHub Releases.

## Anti-goals

- A rollout declared successful without a passed analysis.
- Agents able to change deploy config or trigger deploys.
- A silent degradation: every failed analysis must produce a rollback plus a postmortem.

## Left to the builder

- Exact PromQL text.
- The argo-rollouts chart version (pin a release at least two weeks old).
- The k6 script details.
- How the deployer reads AnalysisRuns (kubectl JSON).
- Postmortem wording.
