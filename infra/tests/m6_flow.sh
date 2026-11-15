#!/usr/bin/env bash
# Acceptance M6: merged change → Argo Rollouts canary in prod with metric analysis.
#
#   GOOD path: an agent task on swarm-lab/isv-product goes through the corridor
#     (selfdev_flow.sh: PR → CI → bot review → HITL merge, auto-approved here as
#     in m5) and the worker's deployer rolls the merged main out as a canary
#     (20→50→100 %, canary-vs-stable + post-rollout analysis). Assert: verdict
#     succeeded, Rollout Healthy on isv-product:<sha7>, all analysis runs passed.
#   BAD path: the same commit is deployed with an injected fault
#     (FAULT_ERROR_RATE=0.5). Assert: verdict rolled_back, a failed analysis
#     run, postmortem.md written, prod back to the stable version (Healthy,
#     faults off).
#
# Preconditions: bootstrap sections rollouts, monitoring (Prometheus NodePort
# 30090), demo (isv-product Healthy), mcp (observability).
# Usage: ./infra/scripts/toolbox.sh bash infra/tests/m6_flow.sh
set -euo pipefail

fail() { echo "M6 flow: FAIL — $1"; exit 1; }
cd "$(dirname "$0")/../.."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PROM=http://localhost:30090

# --- preflight ------------------------------------------------------------------
kubectl -n platform rollout status deploy/argo-rollouts --timeout=60s >/dev/null \
  || fail "argo-rollouts controller not ready (bootstrap.sh rollouts)"
[ "$(kubectl -n prod get rollout isv-product -o jsonpath='{.status.phase}')" = "Healthy" ] \
  || fail "rollout isv-product is not Healthy (bootstrap.sh demo)"
curl -sf -m 5 "$PROM/-/ready" >/dev/null || fail "Prometheus not on $PROM (bootstrap.sh monitoring)"
kubectl -n agents rollout status deploy/observability --timeout=60s >/dev/null \
  || fail "observability MCP server not ready (bootstrap.sh mcp)"
BEFORE_IMAGE="$(kubectl -n prod get rollout isv-product -o jsonpath='{.spec.template.spec.containers[0].image}')"
echo "M6 flow: preflight ok — prod on $BEFORE_IMAGE"

# --- GOOD path: agent change → corridor → canary ------------------------------------
SELFDEV_REPO=isv-product \
SELFDEV_TITLE="Add GET /api/time" \
SELFDEV_BODY="In src/isv_product/main.py, inside create_app, add an endpoint GET /api/time returning {\"time\": <current UTC time as an ISO 8601 string>} (use datetime.now(UTC).isoformat()). Add a pytest test in tests/test_time.py using fastapi.testclient.TestClient(create_app(error_rate=0.0, latency_ms=0)) that checks status 200 and that the value parses with datetime.fromisoformat. Do not touch deploy/." \
SELFDEV_VERTEX="add GET /api/time returning the current UTC ISO time; test in tests/test_time.py" \
SELFDEV_DONE_CMD="uv sync --quiet --all-groups && uv run --quiet pytest -q" \
SELFDEV_PATHS="src/**,tests/**" \
SELFDEV_DEPLOY=1 \
SELFDEV_AUTO_APPROVE_MERGE=1 \
SELFDEV_RESULT_FILE="$TMP/result.json" \
  bash infra/tests/selfdev_flow.sh | tee "$TMP/selfdev.log" | grep -E "Selfdev flow: (run=|HITL|PR |PASS|deploy|FAIL)" || true
grep -q "Selfdev flow: PASS" "$TMP/selfdev.log" || fail "the corridor run did not pass (see above)"

python3 - "$TMP/result.json" > "$TMP/good.env" <<'PY' || fail "no successful deploy verdict on the vertex"
import json, sys
verdict = json.load(open(sys.argv[1])).get("deploy_verdict") or {}
assert verdict.get("status") == "succeeded", verdict
runs = verdict.get("analysis") or []
assert runs and all(r["phase"] == "Successful" for r in runs), runs
print(f"GOOD_IMAGE={verdict['image']}")
PY
. "$TMP/good.env"
LIVE="$(kubectl -n prod get rollout isv-product -o jsonpath='{.status.phase} {.spec.template.spec.containers[0].image}')"
[ "$LIVE" = "Healthy $GOOD_IMAGE" ] || fail "prod is '$LIVE', expected Healthy on $GOOD_IMAGE"
echo "M6 flow: GOOD — canary $GOOD_IMAGE promoted after passing analysis (was $BEFORE_IMAGE)"

# --- BAD path: same commit with an injected fault → rollback ----------------------
ADMIN_TOKEN="$(kubectl -n platform exec "$(kubectl -n platform get pod -l app.kubernetes.io/name=openbao -o name)" -- \
  env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$(cat infra/.vault-root-token)" bao kv get -field=token secret/gitea/admin)"
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="Authorization: token $ADMIN_TOKEN" \
  git clone -q http://localhost:30080/swarm-lab/isv-product.git "$TMP/repo"
SHA="$(git -C "$TMP/repo" rev-parse HEAD)"
set +e
uv run python -m agent_runtime.deployer --sha "$SHA" --repo-dir "$TMP/repo" --prometheus "$PROM" \
  --evidence-dir "$TMP/bad" --fault FAULT_ERROR_RATE=0.5 > "$TMP/bad.json"
BAD_RC=$?
set -e
[ "$BAD_RC" = "1" ] || fail "the fault deploy must exit 1 (got $BAD_RC)"
python3 - "$TMP/bad.json" <<'PY' || fail "the fault deploy was not rolled back by analysis"
import json, sys
verdict = json.load(open(sys.argv[1]))
assert verdict["status"] == "rolled_back", verdict["status"]
failed = [r for r in verdict["analysis"] if r["phase"] == "Failed"]
assert failed, verdict["analysis"]
print("M6 flow: BAD — rolled back; failed:", [(r["name"], [m["name"] for m in r["metrics"] if m["phase"] == "Failed"]) for r in failed])
PY
[ -s "$TMP/bad/postmortem.md" ] || fail "no postmortem.md for the rollback"
for _ in $(seq 1 30); do
  STATE="$(kubectl -n prod get rollout isv-product -o jsonpath='{.status.phase} {.spec.template.spec.containers[0].env[0].value}')"
  [ "$STATE" = "Healthy 0" ] && break
  sleep 5
done
[ "$STATE" = "Healthy 0" ] || fail "prod did not return to the stable version (state: $STATE)"
echo "M6 flow: BAD — postmortem written; prod back to stable $GOOD_IMAGE, faults off"
echo "M6 flow: PASS"
