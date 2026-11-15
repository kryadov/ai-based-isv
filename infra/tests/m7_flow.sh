#!/usr/bin/env bash
# Acceptance M7: project memory closes the loop "experience → next task".
#
#   1. A bad canary on swarm-lab/isv-product (deployer CLI, FAULT_ERROR_RATE=0.5)
#      is rolled back and its postmortem is written to PROJECT MEMORY.
#   2. The next agent task on the same project (selfdev_flow.sh, auto-approved
#      merge, no deploy) gets that postmortem in its prompt: the session log
#      carries `memory_slice` with the episode id; after the merge the
#      memory-extractor may add lessons.
#   3. The postmortem is promoted to org memory: a PR in swarm-lab/org-memory.
#
# The control plane (agent_api) of steps 1 and 3 runs on M7_API_PORT; the
# selfdev flow starts its own on 8472 — both on the same Postgres DB, so the
# memory is shared.
# Preconditions: sections rollouts, monitoring, demo, mcp, org-memory.
# Usage: ./infra/scripts/toolbox.sh bash infra/tests/m7_flow.sh
set -euo pipefail

fail() { echo "M7 flow: FAIL — $1"; exit 1; }
cd "$(dirname "$0")/../.."

NS=platform
PROM=http://localhost:30090
API_PORT="${M7_API_PORT:-8473}"
API="http://127.0.0.1:$API_PORT"
PROJECT=swarm-lab/isv-product
TMP="$(mktemp -d)"
API_PID=""
cleanup() {
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

start_api() {
  local pw
  pw="$(kubectl -n "$NS" get secret postgres-platform-app -o jsonpath='{.data.password}' | base64 -d)"
  AGENT_API_DATABASE_URL="postgresql+asyncpg://gitea:$pw@localhost:15432/swarm" \
    uv run uvicorn --factory agent_api.main:create_app --host 127.0.0.1 --port "$API_PORT" \
    > "$TMP/agent-api.log" 2>&1 &
  API_PID=$!
  for _ in $(seq 1 30); do curl -sf -m 2 -o /dev/null "$API/healthz" && return 0; sleep 1; done
  tail -20 "$TMP/agent-api.log"; fail "agent_api did not start on $API"
}
stop_api() { kill "$API_PID" 2>/dev/null || true; wait "$API_PID" 2>/dev/null || true; API_PID=""; }

# --- preflight --------------------------------------------------------------------
[ "$(kubectl -n prod get rollout isv-product -o jsonpath='{.status.phase}')" = "Healthy" ] \
  || fail "rollout isv-product is not Healthy (bootstrap.sh demo)"
curl -sf -m 5 "$PROM/-/ready" >/dev/null || fail "Prometheus not on $PROM"
ADMIN_TOKEN="$(kubectl -n "$NS" exec "$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=openbao -o name)" -- \
  env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$(cat infra/.vault-root-token)" bao kv get -field=token secret/gitea/admin)"
AUTH="Authorization: token $ADMIN_TOKEN"
curl -sf -o /dev/null -H "$AUTH" http://localhost:30080/api/v1/repos/swarm-lab/org-memory \
  || fail "swarm-lab/org-memory missing (bootstrap.sh org-memory)"
echo "M7 flow: preflight ok"

# --- 1) a rolled-back canary writes a postmortem episode --------------------------
start_api
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="$AUTH" \
  git clone -q http://localhost:30080/swarm-lab/isv-product.git "$TMP/repo"
SHA="$(git -C "$TMP/repo" rev-parse HEAD)"
set +e
uv run python -m agent_runtime.deployer --sha "$SHA" --repo-dir "$TMP/repo" --prometheus "$PROM" \
  --fault FAULT_ERROR_RATE=0.5 --memory-api "$API" --project "$PROJECT" > "$TMP/bad.json"
set -e
python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); assert v["status"]=="rolled_back", v["status"]' "$TMP/bad.json" \
  || fail "the fault canary was not rolled back"
curl -sf "$API/memory/slice" --get --data-urlencode "project=$PROJECT" \
  --data-urlencode "paths=src/**" --data-urlencode "q=canary rolled back" > "$TMP/slice1.json"
PM_ID="$(python3 -c '
import json, sys
eps = [e for e in json.load(open(sys.argv[1]))["episodes"] if e["kind"] == "postmortem"]
assert eps, "no postmortem episode in project memory"
print(max(e["id"] for e in eps))' "$TMP/slice1.json")" || fail "no postmortem episode in project memory"
echo "M7 flow: rollback recorded as postmortem episode #$PM_ID"
stop_api

# --- 2) the next task on the project gets it in its prompt -------------------------
SELFDEV_REPO=isv-product \
SELFDEV_TITLE="Add GET /api/version" \
SELFDEV_BODY="In src/isv_product/main.py, inside create_app, add GET /api/version returning {\"version\": \"0.1.0\"}. Add a pytest test in tests/test_version.py using fastapi.testclient.TestClient(create_app(error_rate=0.0, latency_ms=0)). Do not touch deploy/." \
SELFDEV_VERTEX="add GET /api/version returning the package version; test in tests/test_version.py" \
SELFDEV_DONE_CMD="uv sync --quiet --all-groups && uv run --quiet pytest -q" \
SELFDEV_PATHS="src/**,tests/**" \
SELFDEV_AUTO_APPROVE_MERGE=1 \
  bash infra/tests/selfdev_flow.sh | tee "$TMP/selfdev.log" | grep -E "Selfdev flow: (run=|HITL|PASS|FAIL)" || true
grep -q "Selfdev flow: PASS" "$TMP/selfdev.log" || fail "the corridor run did not pass"
read -r RUN_ID VERTEX_ID < <(sed -n 's/.*run=\([0-9]*\) vertex=\([0-9]*\).*/\1 \2/p' "$TMP/selfdev.log" | head -1)
LOG=".agent-runtime-workspace/run-$RUN_ID/.agent_runtime/runs/run-$RUN_ID/vertex-$VERTEX_ID/session_log.ndjson"
python3 - "$LOG" "$PM_ID" <<'PY' || fail "the task did not receive the postmortem from project memory"
import json, sys
path, pm = sys.argv[1], int(sys.argv[2])
slices = [json.loads(l)["payload"] for l in open(path, encoding="utf-8") if '"memory_slice"' in l]
assert slices and pm in slices[0]["episodes"], slices
print(f"M7 flow: vertex prompt carried project memory episodes {slices[0]['episodes']}")
PY

# --- 3) promotion to org memory -------------------------------------------------------
start_api
uv run org-bootstrap memory promote --episode "$PM_ID" --api "$API" | tee "$TMP/promote.log"
grep -q "pulls/" "$TMP/promote.log" || fail "no promotion PR URL"
LESSONS="$(curl -sf "$API/memory/slice" --get --data-urlencode "project=$PROJECT" \
  --data-urlencode "paths=src/**" --data-urlencode "q=version endpoint" \
  | python3 -c 'import json,sys; print(sum(1 for e in json.load(sys.stdin)["episodes"] if e["kind"]=="lesson"))')"
echo "M7 flow: lessons for the project after the task: $LESSONS (memory-extractor, may be 0)"
echo "M7 flow: PASS"
