#!/usr/bin/env bash
# Acceptance M8 (swarm mode): one task → a planned vertex graph → parallel
# workers → every vertex through the corridor → judge.
#
#   1. an issue on swarm-lab/isv-product asks for three independent utility
#      modules plus a doc page listing them; the poller creates the run;
#   2. `swarm plan` (coordinator LLM, validated graph, platform done_when);
#   3. `swarm run --workers 3`: separate lease identities and workspaces;
#      merges auto-approved (acceptance only, as in m5);
#   4. assert: >= 2 vertices, >= 1 with deps; all done; >= 2 leases overlapped;
#      the dependent vertex prompt carried the handoff of its deps;
#   5. `swarm judge`: all done + merged main passes the done command.
#
# Usage: ./infra/scripts/toolbox.sh bash infra/tests/m8_flow.sh
set -euo pipefail

fail() { echo "M8 flow: FAIL — $1"; exit 1; }

cd "$(dirname "$0")/../.."

NS=platform
GITEA=http://localhost:30080
LITELLM=http://localhost:30084
ORG=swarm-lab
REPO=isv-product
PG_HOST=localhost
PG_PORT=15432
PG_DB=swarm
PG_USER=gitea
API_PORT="${M4C_API_PORT:-8472}"
API_URL="http://127.0.0.1:$API_PORT"
WORKER_AGENT=swarm
WORKSPACE_DIR="$PWD/.agent-runtime-workspace"
POLL_TIMEOUT_SECONDS=7200   # 2 h: the human HITL decision is inside this window
SESSION_TIMEOUT_SECONDS=600 # per attempt: a looping local model fails the attempt, not the flow
POLL_INTERVAL_SECONDS=10

# The lab gateway (LiteLLM) routes its aliases to an external API (OpenRouter;
# infra/litellm/deployment.yaml). The harness needs a model that emits real
# OpenAI tool_calls (spike lesson 11: a model that answers with tool-call JSON as
# text leaves the session diff empty); alias `qwen3` is the tool-calling agent
# model this flow drives.
MODEL="litellm/qwen3"
MODEL_ALIAS="${MODEL#litellm/}"

PINNED_IMAGE=""
DERIVED_IMAGE=""
API_PID=""
WORKER_PID=""
TMP="$(mktemp -d)"

cleanup() {
  if [ -n "$WORKER_PID" ]; then
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
  if [ -n "$API_PID" ]; then
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
  # `docker run --rm` removes the container when it stops; a crashed flow can still
  # leave one behind, so sweep the derived image's containers explicitly.
  if [ -n "$DERIVED_IMAGE" ]; then
    for cid in $(docker ps -aq --filter "ancestor=$DERIVED_IMAGE" 2>/dev/null); do
      docker rm -f "$cid" >/dev/null 2>&1 || true
    done
  fi
  # A killed worker cannot tear its sandbox down; sweep this run's objects
  # (pod/service/secret carry the run-id label) so nothing leaks credentials.
  if [ -n "${RUN_ID:-}" ]; then
    kubectl -n agents delete pod,service,secret -l "agent-runtime.io/run-id=$RUN_ID"       --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  # netpol probe pods (sleep 300) — a failing assertion must not leave them behind
  if [ -n "${RUN_ID:-}" ]; then
    kubectl -n agents delete pod "selfdev-netpol-probe-$RUN_ID" --force --grace-period=0 --ignore-not-found >/dev/null 2>&1 || true
    kubectl -n default delete pod "selfdev-outside-probe-$RUN_ID" --force --grace-period=0 --ignore-not-found >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1) Preflight — every live component the flow depends on
# ---------------------------------------------------------------------------
echo "M8 flow: preflight — live stand"

# shellcheck source=../scripts/cluster-lib.sh
. infra/scripts/cluster-lib.sh
kubectl get nodes >/dev/null 2>&1 || fail "stand cluster ($(swarm_cluster)) is not reachable"
docker info >/dev/null 2>&1 || fail "docker is not reachable on the host"

# The worker writes a full clone of the target repo plus the run evidence under the
# workspace; neither may ever enter this repository's history (AGENTS.md §9).
git check-ignore -q "$WORKSPACE_DIR" \
  || fail "$WORKSPACE_DIR is not gitignored — add it to .gitignore first"
git check-ignore -q .agent_runtime \
  || fail ".agent_runtime is not gitignored — add it to .gitignore first"

# Pinned executor image — single source of truth is agent_runtime.settings.
PINNED_IMAGE="$(uv run python -c 'from agent_runtime.settings import OPENCODE_IMAGE; print(OPENCODE_IMAGE)')"
[ -n "$PINNED_IMAGE" ] || fail "could not read the pinned image from agent_runtime.settings"
DERIVED_IMAGE="opencode-git:${PINNED_IMAGE##*:}"
docker image inspect "$PINNED_IMAGE" >/dev/null 2>&1 \
  || docker pull "$PINNED_IMAGE" >/dev/null 2>&1 \
  || fail "pinned image $PINNED_IMAGE is neither present locally nor pullable"

# --- k3s sandbox infrastructure (section_agents) ----------------------------
uv run org-bootstrap apply --section agents >/dev/null 2>&1 \
  || fail "section_agents failed to apply (namespace/PVC/NetworkPolicy)"
kubectl -n agents get pvc agent-scratch >/dev/null 2>&1 \
  || fail "PVC agent-scratch missing in namespace agents"
cluster_nodeport_published 30100 \
  || fail "the cluster does not publish the 30100-30110 NodePort range (section_agents should add it)"
# The derived image must be visible to k3s pods (import is idempotent; no-op on Rancher Desktop).
cluster_image_import "$DERIVED_IMAGE" >/dev/null 2>&1 \
  || fail "could not make $DERIVED_IMAGE available to the cluster"

# --- MCP servers (section_mcp): task-source + llm in `agents`, NodePort 30085 ---
uv run org-bootstrap apply --section mcp > "$TMP/section-mcp.log" 2>&1 \
  || { tail -20 "$TMP/section-mcp.log"; fail "section_mcp failed to apply"; }
cluster_nodeport_published 30085 \
  || fail "the cluster does not publish NodePort 30085 (section_mcp should add it)"
# The host worker writes through task-source (NodePort); the sandbox harness only
# ever sees the read-only task-reader (review I3). The write server's in-cluster
# URL is used below solely to prove pods are DENIED (review I2).
TASK_SOURCE_HOST_URL="http://localhost:30085/mcp"
TASK_READER_POD_URL="http://task-reader.agents.svc.cluster.local:8000/mcp"
TASK_SOURCE_POD_URL="http://task-source.agents.svc.cluster.local:8000/mcp"
LLM_POD_URL="http://llm.agents.svc.cluster.local:8000/mcp"

# Gitea admin token (OpenBao secret/gitea/admin, same source as m2a/m2c).
ROOT_TOKEN_FILE="infra/.vault-root-token"
[ -f "$ROOT_TOKEN_FILE" ] || fail "$ROOT_TOKEN_FILE is missing — org-bootstrap apply writes it"
VAULT_POD="$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=openbao -o jsonpath='{.items[0].metadata.name}')"
[ -n "$VAULT_POD" ] || fail "no OpenBao pod in namespace $NS"
# A host restart seals OpenBao; unseal it (keys stay gitignored) so the token
# reads below succeed — the spike hit this exact state on 2026-11-10.
SEALED="$(kubectl -n "$NS" exec "$VAULT_POD" -- env BAO_ADDR=http://127.0.0.1:8200 bao status 2>/dev/null | grep -c 'Sealed[[:space:]]*true' || true)"
if [ "${SEALED:-0}" -ge 1 ]; then
  UNSEAL_KEY="$(python3 -c 'import json;print(json.load(open("infra/.vault-unseal-keys.json"))["unseal_keys_b64"][0])')" \
    || fail "OpenBao is sealed and infra/.vault-unseal-keys.json is missing"
  kubectl -n "$NS" exec "$VAULT_POD" -- env BAO_ADDR=http://127.0.0.1:8200 \
    bao operator unseal "$UNSEAL_KEY" >/dev/null \
    || fail "OpenBao unseal failed"
  echo "M8 flow: preflight: OpenBao unsealed"
fi
TOKEN="$(kubectl -n "$NS" exec "$VAULT_POD" -- \
  env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$(cat "$ROOT_TOKEN_FILE")" \
  bao kv get -field=token secret/gitea/admin)"
[ -n "$TOKEN" ] || fail "no Gitea admin token in OpenBao secret/gitea/admin"
AUTH_HEADER="Authorization: token $TOKEN"
code="$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH_HEADER" "$GITEA/api/v1/user")"
[ "$code" = "200" ] || fail "Gitea API unreachable (user=$code)"

# LiteLLM master key + the two model properties the harness depends on.
MK="$(kubectl -n "$NS" exec "$VAULT_POD" -- \
  env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$(cat "$ROOT_TOKEN_FILE")" \
  bao kv get -field=master-key secret/litellm/master)"
[ -n "$MK" ] || fail "no LiteLLM master key in OpenBao secret/litellm/master"
python3 - "$MODEL_ALIAS" "$TMP" <<'PY'
import json, sys
alias, tmp = sys.argv[1], sys.argv[2]
plain = {
    "model": alias,
    "messages": [{"role": "user", "content": "Reply with exactly: GATEWAY-OK"}],
    "max_tokens": 256,
}
tool = {
    "model": alias,
    "messages": [{"role": "user", "content": "Create a file named probe.txt containing PROBE-OK."}],
    "max_tokens": 256,
    "tools": [{
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write a file to disk",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    }],
}
with open(f"{tmp}/litellm-plain.json", "w", encoding="utf-8") as handle:
    handle.write(json.dumps(plain))
with open(f"{tmp}/litellm-tools.json", "w", encoding="utf-8") as handle:
    handle.write(json.dumps(tool))
PY
curl -sf -m 300 -X POST "$LITELLM/v1/chat/completions" \
  -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' \
  --data @"$TMP/litellm-plain.json" > "$TMP/litellm-plain-out.json" \
  || fail "LiteLLM gateway unreachable at $LITELLM"
python3 - "$TMP/litellm-plain-out.json" <<'PY' || fail "no completion from the gateway"
import json, sys
message = json.load(open(sys.argv[1]))["choices"][0]["message"]
assert (message.get("content") or message.get("reasoning_content") or "").strip(), message
print("preflight: gateway completion ok")
PY
# The harness can only drive a model that emits real OpenAI tool_calls: qwen2.5-coder:7b
# answers with the raw tool-call JSON as assistant text, so the sandbox never touches a
# file and the session diff comes back empty (spike lesson 11). Assert the property here
# rather than discovering it after the 20-minute vertex budget.
curl -sf -m 300 -X POST "$LITELLM/v1/chat/completions" \
  -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' \
  --data @"$TMP/litellm-tools.json" > "$TMP/litellm-tools-out.json" \
  || fail "LiteLLM gateway unreachable at $LITELLM"
python3 - "$TMP/litellm-tools-out.json" "$MODEL_ALIAS" <<'PY' || fail "the model behind $MODEL_ALIAS does not emit tool_calls — the harness cannot change any file"
import json, sys
message = json.load(open(sys.argv[1]))["choices"][0]["message"]
assert message.get("tool_calls"), message
print(f"preflight: model {sys.argv[2]} emits tool_calls")
PY

# Postgres NodePort (CNPG database `swarm`) — no port-forward (M3 lesson).
PGPW="$(kubectl -n "$NS" get secret postgres-platform-app -o jsonpath='{.data.password}' | base64 -d)"
[ -n "$PGPW" ] || fail "no Postgres app password in secret postgres-platform-app"
psql_q() {
  PGPASSWORD="$PGPW" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
    -Atc "$1" 2>/dev/null || true
}
PG_OK=0
for _ in 1 2 3 4 5; do
  [ "$(psql_q 'SELECT 1')" = "1" ] && { PG_OK=1; break; }
  sleep 2
done
[ "$PG_OK" = "1" ] || fail "Postgres NodePort $PG_PORT (db $PG_DB) is unreachable"

# ---------------------------------------------------------------------------
# 2) The platform repo must exist and be protected (platform-sync.sh init)
# ---------------------------------------------------------------------------
code="$(curl -s -o "$TMP/repo.json" -w '%{http_code}' -H "$AUTH_HEADER" "$GITEA/api/v1/repos/$ORG/$REPO")"
[ "$code" = "200" ] || fail "$ORG/$REPO missing — run: bash infra/scripts/platform-sync.sh init (host Git Bash)"
SEED_SHA="$(curl -fsS -H "$AUTH_HEADER" "$GITEA/api/v1/repos/$ORG/$REPO/branches/main" | python3 -c 'import json,sys; print(json.load(sys.stdin)["commit"]["id"])')"
[ -n "$SEED_SHA" ] || fail "could not read main of $ORG/$REPO"
curl -fsS -o /dev/null -H "$AUTH_HEADER" "$GITEA/api/v1/repos/$ORG/$REPO/branch_protections/main" \
  || fail "main of $ORG/$REPO is not protected — re-run platform-sync.sh init"
echo "M8 flow: $ORG/$REPO main at ${SEED_SHA:0:7}, protected"

# ---------------------------------------------------------------------------
# 3) Control plane: run + vertex, on the live Postgres
# ---------------------------------------------------------------------------
echo "M8 flow: starting agent_api on $API_URL"
if curl -s -m 2 -o /dev/null "$API_URL/healthz"; then
  fail "something already listens on port $API_PORT — set M4C_API_PORT to a free port"
fi
AGENT_API_DATABASE_URL="postgresql+asyncpg://$PG_USER:$PGPW@$PG_HOST:$PG_PORT/$PG_DB" \
  uv run uvicorn --factory agent_api.main:create_app --host 127.0.0.1 --port "$API_PORT" \
  > "$TMP/agent-api.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 30); do
  curl -sf -m 2 -o /dev/null "$API_URL/healthz" && break
  sleep 1
done
curl -sf -m 5 "$API_URL/healthz" >/dev/null \
  || { tail -20 "$TMP/agent-api.log"; fail "agent_api did not start on $API_URL"; }

# --- the task: a Gitea issue on the platform repo ------------------------------

SELFDEV_TITLE="${SELFDEV_TITLE:-Add text utilities (slug, word count, reverse words) and document them}"
SELFDEV_BODY="${SELFDEV_BODY:-Create three INDEPENDENT modules, each with its own pytest file, then document them. (1) src/isv_product/slug.py: slugify(text) -> lowercase alphanumeric words joined by '-'; tests/test_slug.py. (2) src/isv_product/wordcount.py: word_count(text) -> number of whitespace-separated words; tests/test_wordcount.py. (3) src/isv_product/reverse.py: reverse_words(text) -> the words in reverse order joined by single spaces; tests/test_reverse.py. (4) AFTER the three modules exist: docs/UTILITIES.md listing the three functions with one example each. Do not touch other files.}"
python3 - "$SELFDEV_TITLE" "$SELFDEV_BODY" > "$TMP/issue.json" <<'PYJ'
import json, sys
print(json.dumps({"title": sys.argv[1], "body": sys.argv[2]}))
PYJ
ISSUE_JSON="$(curl -sf -H "$AUTH_HEADER" -H 'Content-Type: application/json' -X POST \
  "$GITEA/api/v1/repos/$ORG/$REPO/issues" \
  --data @"$TMP/issue.json")" \
  || fail "could not create the task issue in $ORG/$REPO"
ISSUE_NUMBER="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["number"])' <<<"$ISSUE_JSON")"
TASK_REF="$ORG/$REPO#$ISSUE_NUMBER"
echo "M8 flow: task issue $TASK_REF"

# --- poller: open tasks via the task-source MCP -> one run per task (dedup) -----
# The m2c poller pattern, adapted: the task list comes from the MCP server (one
# stateless JSON-RPC POST, the shape the worker's TrackerClient uses) and every
# new task_ref becomes a run; the checkpoint makes a re-poll a no-op.
cat > "$TMP/poller.py" <<'PY'
import json, sys, urllib.request
mcp_url, api_url, prefix, checkpoint = sys.argv[1:5]

def post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)

rpc = post(mcp_url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "list_tasks", "arguments": {"task_ref_prefix": prefix}}},
           {"Accept": "application/json, text/event-stream"})
result = rpc["result"]
assert not result.get("isError"), result
tasks = result["structuredContent"]["result"]
try:
    seen = set(json.load(open(checkpoint)))
except FileNotFoundError:
    seen = set()
delivered = 0
for task in tasks:
    ref = task["task_ref"]
    if ref in seen:
        continue
    run = post(f"{api_url}/runs", {"project": "swarm", "task_ref": ref}, {})
    print(f"run {run['run_id']} for {ref}")
    seen.add(ref)
    delivered += 1
json.dump(sorted(seen), open(checkpoint, "w"))
print(f"delivered {delivered}")
PY
CHECKPOINT="$TMP/poller-checkpoint.json"
POLL1="$(uv run python "$TMP/poller.py" "$TASK_SOURCE_HOST_URL" "$API_URL" "$ORG/$REPO" "$CHECKPOINT")" \
  || fail "the poller failed (task-source MCP or agent_api)"
echo "$POLL1" | grep -q "delivered 1" || fail "the poller must deliver exactly the new task ($POLL1)"
RUN_ID="$(echo "$POLL1" | awk -v ref="$TASK_REF" '$1=="run" && $4==ref {print $2}')"
[ -n "$RUN_ID" ] || fail "no run created for $TASK_REF ($POLL1)"
POLL2="$(uv run python "$TMP/poller.py" "$TASK_SOURCE_HOST_URL" "$API_URL" "$ORG/$REPO" "$CHECKPOINT")" \
  || fail "the second poll failed"
echo "$POLL2" | grep -q "delivered 0" || fail "re-poll created duplicate runs ($POLL2)"
RUNS_FOR_TASK="$(psql_q "SELECT count(*) FROM runs WHERE task_ref='$TASK_REF' AND id >= $RUN_ID")"
[ "$RUNS_FOR_TASK" = "1" ] || fail "expected exactly one run for $TASK_REF, found $RUNS_FOR_TASK"
DB_TASK_REF="$(psql_q "SELECT task_ref FROM runs WHERE id=$RUN_ID")"
[ "$DB_TASK_REF" = "$TASK_REF" ] || fail "run $RUN_ID carries task_ref '$DB_TASK_REF', expected $TASK_REF"
echo "M8 flow: poller created run $RUN_ID for $TASK_REF; re-poll deduplicated"

# ---------------------------------------------------------------------------
# 4) Coordinator: plan the graph (validated; platform-supplied done_when)
# ---------------------------------------------------------------------------
DONE_CMD="uv sync --quiet --all-groups && uv run --quiet pytest -q"
export AGENT_RUNTIME_API_URL="$API_URL" AGENT_RUNTIME_RUN_ID="$RUN_ID" \
  AGENT_RUNTIME_AGENT="$WORKER_AGENT" AGENT_RUNTIME_WORKSPACE_DIR="$WORKSPACE_DIR/swarm-$RUN_ID" \
  AGENT_RUNTIME_REPO_URL="$GITEA/$ORG/$REPO.git" AGENT_RUNTIME_BASE_BRANCH=main \
  AGENT_RUNTIME_SANDBOX_BACKEND=k3s AGENT_RUNTIME_K8S_NAMESPACE=agents \
  AGENT_RUNTIME_LITELLM_BASE_URL="http://litellm.platform.svc.cluster.local:4000/v1" \
  AGENT_RUNTIME_LITELLM_API_KEY="$MK" AGENT_RUNTIME_MODEL="$MODEL" \
  AGENT_RUNTIME_SESSION_TIMEOUT_SECONDS="$SESSION_TIMEOUT_SECONDS" \
  AGENT_RUNTIME_MCP_TASK_SOURCE_URL="$TASK_READER_POD_URL" AGENT_RUNTIME_MCP_LLM_URL="$LLM_POD_URL" \
  AGENT_RUNTIME_MCP_OBSERVABILITY_URL="http://observability.agents.svc.cluster.local:8000/mcp" \
  AGENT_RUNTIME_TASK_REF="$TASK_REF" AGENT_RUNTIME_TASK_SOURCE_URL="$TASK_SOURCE_HOST_URL" \
  AGENT_RUNTIME_GATES_ENABLED=1 AGENT_RUNTIME_CI_TIMEOUT_S=1500 AGENT_RUNTIME_HITL_POLL_S=5 \
  AGENT_RUNTIME_REVIEWER_BASE_URL="http://localhost:30084/v1" AGENT_RUNTIME_DEPLOY_ENABLED=0
mkdir -p "$AGENT_RUNTIME_WORKSPACE_DIR"
uv run python -m agent_runtime.swarm plan --run-id "$RUN_ID" --title "$SELFDEV_TITLE" \
  --body "$SELFDEV_BODY" --allowed "src/**,tests/**,docs/**" --done-cmd "$DONE_CMD" | tee "$TMP/plan.txt"
curl -fsS "$API_URL/runs/$RUN_ID/vertices" > "$TMP/graph.json"
python3 - "$TMP/graph.json" <<'PY' || fail "the planner did not produce a parallel graph (fell back to one vertex?)"
import json, sys
graph = json.load(open(sys.argv[1]))["vertices"]
assert len(graph) >= 2, graph
assert any(v["deps"] for v in graph), "no dependent vertex"
assert sum(1 for v in graph if not v["deps"]) >= 2, "fewer than two parallel vertices"
print(f"M8 flow: plan — {len(graph)} vertices, {sum(1 for v in graph if v['deps'])} dependent")
PY

# ---------------------------------------------------------------------------
# 5) Swarm: 3 workers; merges auto-approved (acceptance only)
# ---------------------------------------------------------------------------
( while :; do
    for id in $(curl -fsS "$API_URL/hitl/pending" 2>/dev/null | python3 -c 'import json,sys; print(" ".join(str(r["id"]) for r in json.load(sys.stdin)["pending"] if r["kind"]=="merge"))' 2>/dev/null); do
      curl -fsS -o /dev/null -X POST "$API_URL/hitl/$id/decision" -H "Content-Type: application/json" \
        -d '{"action":"approve","decided_by":"acceptance-flow"}' && echo "M8 flow: HITL #$id auto-approved"
    done
    sleep 5
  done ) &
APPROVER_PID=$!
START_TS="$(date +%s)"
uv run python -m agent_runtime.swarm run --workers 3 --timeout 5400 | tee "$TMP/run.txt" || true
kill "$APPROVER_PID" 2>/dev/null || true
ELAPSED=$(( $(date +%s) - START_TS ))

# ---------------------------------------------------------------------------
# 6) Evidence: all done, parallelism, handoffs, judge
# ---------------------------------------------------------------------------
curl -fsS "$API_URL/runs/$RUN_ID/vertices" > "$TMP/graph.json"
curl -fsS "$API_URL/audit?limit=500" > "$TMP/audit.json"
python3 - "$TMP/graph.json" "$TMP/audit.json" "$AGENT_RUNTIME_WORKSPACE_DIR" "$RUN_ID" <<'PY' || fail "swarm evidence check failed"
import glob, json, sys
graph = json.load(open(sys.argv[1]))["vertices"]
events = json.load(open(sys.argv[2]))["events"]
root, run_id = sys.argv[3], sys.argv[4]
bad = [(v["name"], v["status"]) for v in graph if v["status"] != "done"]
assert not bad, f"vertices not done: {bad}"
ids = {f"vertex:{v['id']}": v["name"] for v in graph}
spans = {}
for e in sorted(events, key=lambda e: e["id"]):
    if e["target"] in ids and e["action"] in ("claim", "complete"):
        spans.setdefault(e["target"], {})[e["action"]] = e["ts"]
intervals = sorted((s["claim"], s["complete"], t) for t, s in spans.items() if "claim" in s and "complete" in s)
overlaps = [(a[2], b[2]) for i, a in enumerate(intervals) for b in intervals[i + 1:] if b[0] < a[1]]
assert overlaps, f"no two vertices ran concurrently: {intervals}"
print(f"M8 flow: concurrency — overlapping vertices {[(ids[a], ids[b]) for a, b in overlaps]}")
workers = {v["leased_by"] for v in graph if v.get("leased_by")} | {
    e["actor"] for e in events if e["target"] in ids and e["action"] == "claim"}
print(f"M8 flow: workers involved {sorted(w for w in workers if w)}")
dependent = [v for v in graph if v["deps"]]
for v in dependent:
    logs = glob.glob(f"{root}/w*/run-{run_id}/.agent_runtime/runs/run-{run_id}/vertex-{v['id']}/session_log.ndjson")
    assert logs, f"no session log for dependent vertex {v['name']}"
    text = open(logs[0], encoding="utf-8").read()
    assert "Handoff from upstream vertices" in text, f"no handoff in the prompt of {v['name']}"
    for dep in v["deps"]:
        assert f"- {dep}: files" in text, f"handoff of {dep} missing for {v['name']}"
print(f"M8 flow: handoffs present in {len(dependent)} dependent vertex prompt(s)")
PY
AGENT_RUNTIME_GIT_TOKEN="$TOKEN" uv run python -m agent_runtime.swarm judge --run-id "$RUN_ID" \
  --done-cmd "$DONE_CMD" --project "$ORG/$REPO" | tee "$TMP/judge.txt"
grep -q "judge run $RUN_ID: done" "$TMP/judge.txt" || fail "the judge did not pass the run"
curl -fsS "$API_URL/metrics" | grep -E '^swarm_(vertices|runs)' || true
echo "M8 flow: PASS — $TASK_REF -> run $RUN_ID: $(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["vertices"]))' "$TMP/graph.json") vertices by 3 workers in ${ELAPSED}s, judged done"
