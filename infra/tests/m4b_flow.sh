#!/usr/bin/env bash
# Acceptance M4b: executor vertex in a k3s sandbox pod, end to end.
#
# The flow exercises the M4b slice against real infrastructure:
#   1. seed a micro Python project into Gitea (`swarm-lab/target-scratch`);
#   2. create a run + one vertex in agent_api (control plane on live Postgres);
#   3. run the vertex worker with SANDBOX_BACKEND=k3s: it claims the vertex,
#      spawns an `opencode serve` POD (derived git-enabled image, PVC workspace,
#      per-run Secret, NodePort service) behind the default-deny NetworkPolicy,
#      validates done_when/paths_allow by code and pushes the collected diff
#      as `agent/vertex-<id>`;
#   4. assert the evidence chain (branch, session log, manifest), the sandbox
#      teardown, and the network policy (Postgres MUST be unreachable).
#
# Live dependencies, all verified by the preflight below:
#   * k3d cluster `swarm-lab`: Gitea NodePort 30080, LiteLLM NodePort 30084,
#     Postgres NodePort 15432 (CNPG database `swarm`, infra/postgres/nodeport.yaml);
#   * k3s pod infrastructure: `section_agents` (namespace, PVC, NetworkPolicy),
#     the derived `opencode-git` image imported into k3d, and the NodePort
#     range 30100-30110 published through the k3d load balancer;
#   * a `uv run` environment with the platform packages.
#
# Artifacts are kept for inspection under `.agent-runtime-workspace/` (gitignored).
#
# Usage: ./infra/tests/m4b_flow.sh   (M4B_API_PORT overrides the agent_api port)
set -euo pipefail

fail() { echo "M4b flow: FAIL — $1"; exit 1; }

cd "$(dirname "$0")/../.."

NS=platform
GITEA=http://localhost:30080
LITELLM=http://localhost:30084
ORG=swarm-lab
REPO=target-scratch
PG_HOST=localhost
PG_PORT=15432
PG_DB=swarm
PG_USER=gitea
API_PORT="${M4B_API_PORT:-8471}"
API_URL="http://127.0.0.1:$API_PORT"
WORKER_AGENT=m4b-acceptance
WORKSPACE_DIR="$PWD/.agent-runtime-workspace"
POLL_TIMEOUT_SECONDS=1200   # 20 min: the local 7B model needs minutes per turn
POLL_INTERVAL_SECONDS=10

# The lab gateway serves two local Ollama models behind LiteLLM. The harness needs
# a model that emits real OpenAI tool_calls: qwen2.5-coder:7b (alias `qwen-coder`)
# answers with the raw tool-call JSON as assistant text, so the sandbox never
# touches a file and the session diff comes back empty (spike lesson 11, re-confirmed
# live on 2026-11-09 through this gateway). `qwen3` invokes tools reliably and is
# the model this flow drives.
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
  rm -rf "$TMP"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1) Preflight — every live component the flow depends on
# ---------------------------------------------------------------------------
echo "M4b flow: preflight — live stand"

kubectl get nodes >/dev/null 2>&1 || fail "k3d cluster swarm-lab is not reachable"
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
docker port k3d-swarm-lab-serverlb 2>/dev/null | grep -q "30100/tcp" \
  || fail "k3d does not publish the 30100-30110 NodePort range (section_agents should add it)"
# The derived image must be visible to k3s pods (import is idempotent).
k3d image import "$DERIVED_IMAGE" -c swarm-lab >/dev/null 2>&1 \
  || fail "could not import $DERIVED_IMAGE into the k3d cluster"

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
  echo "M4b flow: preflight: OpenBao unsealed"
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
# 2) Seed the scratch repo (idempotent: org and repo are recreated every run)
# ---------------------------------------------------------------------------
echo "M4b flow: seeding $ORG/$REPO in Gitea"
org_code="$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH_HEADER" "$GITEA/api/v1/orgs/$ORG")"
if [ "$org_code" != "200" ]; then
  code="$(curl -s -o "$TMP/org.json" -w '%{http_code}' -H "$AUTH_HEADER" \
    -H 'Content-Type: application/json' -X POST "$GITEA/api/v1/orgs" \
    -d "{\"username\":\"$ORG\",\"visibility\":\"private\"}")"
  [ "$code" = "201" ] || fail "could not create org $ORG ($code): $(cat "$TMP/org.json")"
fi
curl -s -o /dev/null -X DELETE -H "$AUTH_HEADER" "$GITEA/api/v1/repos/$ORG/$REPO" || true
code="$(curl -s -o "$TMP/repo.json" -w '%{http_code}' -H "$AUTH_HEADER" \
  -H 'Content-Type: application/json' -X POST "$GITEA/api/v1/orgs/$ORG/repos" \
  -d "{\"name\":\"$REPO\",\"private\":true,\"auto_init\":false,\"default_branch\":\"main\"}")"
[ "$code" = "201" ] || fail "could not create $ORG/$REPO ($code): $(cat "$TMP/repo.json")"

SEED="$TMP/seed"
mkdir -p "$SEED/src/wordtools" "$SEED/tests"
cat > "$SEED/pyproject.toml" <<'TOML'
[project]
name = "wordtools"
version = "0.1.0"
description = "Micro scratch project seeded by the M4b acceptance flow"
requires-python = ">=3.12"

[tool.pytest.ini_options]
# the module lives under src/; both import roots are accepted so a test may use
# `from wordtools import ...` or `from src.wordtools import ...`
pythonpath = ["src", "."]
testpaths = ["tests"]
TOML
cat > "$SEED/src/wordtools/__init__.py" <<'PY'
"""wordtools — micro scratch package for the M4b acceptance flow."""

__all__ = ["shout"]


def shout(text: str) -> str:
    """Return ``text`` upper-cased with an exclamation mark."""
    return f"{text.upper()}!"
PY
cat > "$SEED/tests/test_wordtools.py" <<'PY'
from wordtools import shout


def test_shout_upper_cases_and_adds_bang() -> None:
    assert shout("hello") == "HELLO!"
PY
cat > "$SEED/.gitignore" <<'IGN'
__pycache__/
*.py[cod]
.pytest_cache/
IGN

(
  cd "$SEED"
  git init -q -b main
  git add pyproject.toml src tests .gitignore
  git -c user.name=m4b-acceptance -c user.email=m4b-acceptance@lab.local \
    commit -qm "m4b: seed micro Python project"
  # credentials travel as an HTTP header — the same scheme the worker's gitops uses
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
    GIT_CONFIG_VALUE_0="Authorization: token $TOKEN" \
    git push -q "$GITEA/$ORG/$REPO.git" main:main
) || fail "could not push the seed to $ORG/$REPO"

seed_ok=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH_HEADER" \
    "$GITEA/api/v1/repos/$ORG/$REPO/branches/main")"
  [ "$code" = "200" ] && { seed_ok=1; break; }
  sleep 1
done
[ "$seed_ok" = "1" ] || fail "seed branch main did not appear in $ORG/$REPO"

# ---------------------------------------------------------------------------
# 3) Control plane: run + vertex, on the live Postgres
# ---------------------------------------------------------------------------
echo "M4b flow: starting agent_api on $API_URL"
if curl -s -m 2 -o /dev/null "$API_URL/healthz"; then
  fail "something already listens on port $API_PORT — set M4B_API_PORT to a free port"
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

RUN_JSON="$(curl -sf -H 'Content-Type: application/json' -X POST "$API_URL/runs" \
  -d '{"project":"m4b-acceptance","task_ref":"M4A-1"}')" || fail "POST /runs failed"
RUN_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])' <<<"$RUN_JSON")"
[ -n "$RUN_ID" ] || fail "no run_id in the POST /runs response"

# The vertex prompt is assembled by the worker from the result-independent vertex
# fields, so the task text travels in `name` (agent_api has no other free field, and
# the column is capped at 120 characters). The wording decides whether the local model
# does the work at all: "reverse_words" alone is read as *character* reversal, and a
# passive phrasing is read as "the file already exists". "add simple tests" matters
# too — left to itself the model keeps inventing whitespace edge cases whose
# expectations contradict its own `text.split()` implementation, and the fail-closed
# done_when check then (correctly) rejects the vertex. ("run pytest" would be
# pointless advice: the sandbox image ships no Python at all, so the done_when check
# is the only thing that ever runs the tests — on the host, not in the sandbox.)
VERTEX_NAME="add-reverse-words: create src/wordtools/reverse.py with reverse_words(text) reversing WORDS (a b->b a); add simple tests"
[ "${#VERTEX_NAME}" -le 120 ] || fail "vertex name exceeds the 120-char column: $VERTEX_NAME"
python3 - "$VERTEX_NAME" > "$TMP/vertex.json" <<'PY'
import json, sys
print(json.dumps({
    "name": sys.argv[1],
    "done_when": json.dumps({"checks": [
        {"type": "cmd", "run": "pytest -q"},
        {"type": "files_exist", "paths": ["src/wordtools/reverse.py"]},
    ]}),
    "paths_allow": ["src/wordtools/**", "tests/**", "pyproject.toml"],
}))
PY
VERTEX_JSON="$(curl -sf -H 'Content-Type: application/json' -X POST \
  "$API_URL/runs/$RUN_ID/vertices" --data @"$TMP/vertex.json")" \
  || fail "POST /runs/$RUN_ID/vertices failed"
VERTEX_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["vertex_id"])' <<<"$VERTEX_JSON")"
[ -n "$VERTEX_ID" ] || fail "no vertex_id in the POST /vertices response"
BRANCH="agent/vertex-$VERTEX_ID"
echo "M4b flow: run=$RUN_ID vertex=$VERTEX_ID branch=$BRANCH"

# ---------------------------------------------------------------------------
# 4) Worker: claim the vertex, spawn opencode serve in docker, push the diff
# ---------------------------------------------------------------------------
mkdir -p "$WORKSPACE_DIR/run-$RUN_ID"
WORKER_LOG="$WORKSPACE_DIR/run-$RUN_ID/worker.log"
# AGENT_RUNTIME_SANDBOX_BACKEND=k3s: the executor attaches to an `opencode serve`
# POD (KubeSandboxExecutorFactory), not a docker container. The LLM endpoint is the
# IN-CLUSTER LiteLLM service — the pod cannot use the host NodePort URL.
env \
  AGENT_RUNTIME_API_URL="$API_URL" \
  AGENT_RUNTIME_RUN_ID="$RUN_ID" \
  AGENT_RUNTIME_AGENT="$WORKER_AGENT" \
  AGENT_RUNTIME_WORKSPACE_DIR="$WORKSPACE_DIR" \
  AGENT_RUNTIME_REPO_URL="$GITEA/$ORG/$REPO.git" \
  AGENT_RUNTIME_BASE_BRANCH=main \
  AGENT_RUNTIME_GIT_TOKEN="$TOKEN" \
  AGENT_RUNTIME_SANDBOX_BACKEND=k3s \
  AGENT_RUNTIME_K8S_NAMESPACE=agents \
  AGENT_RUNTIME_LITELLM_BASE_URL="http://litellm.platform.svc.cluster.local:4000" \
  AGENT_RUNTIME_LITELLM_API_KEY="$MK" \
  AGENT_RUNTIME_MODEL="$MODEL" \
  uv run python -m agent_runtime.worker > "$WORKER_LOG" 2>&1 &
WORKER_PID=$!
echo "M4b flow: worker pid=$WORKER_PID (log: $WORKER_LOG)"

START_TS="$(date +%s)"
STATUS=""
while :; do
  kill -0 "$WORKER_PID" 2>/dev/null \
    || { tail -30 "$WORKER_LOG"; fail "the worker died before the vertex finished"; }
  STATUS="$(psql_q "SELECT status FROM vertices WHERE id=$VERTEX_ID")"
  [ "$STATUS" = "done" ] && break
  ATTEMPTS="$(psql_q "SELECT attempts FROM vertices WHERE id=$VERTEX_ID")"
  if [ "${ATTEMPTS:-0}" -ge 3 ]; then
    reason="$(psql_q "SELECT payload_json->>'reason' FROM audit_events WHERE target='vertex:$VERTEX_ID' AND action='fail' AND payload_json->>'reason' <> 'max_attempts_exceeded' ORDER BY id DESC LIMIT 1")"
    tail -30 "$WORKER_LOG"
    fail "the vertex exhausted its attempts (status=$STATUS, attempts=$ATTEMPTS); last fail reason: ${reason:-none recorded}"
  fi
  [ $(( $(date +%s) - START_TS )) -ge "$POLL_TIMEOUT_SECONDS" ] && break
  sleep "$POLL_INTERVAL_SECONDS"
done
[ "$STATUS" = "done" ] \
  || { tail -30 "$WORKER_LOG"; fail "timeout after ${POLL_TIMEOUT_SECONDS}s (status=${STATUS:-unknown})"; }
ELAPSED=$(( $(date +%s) - START_TS ))
echo "M4b flow: vertex $VERTEX_ID is done after ${ELAPSED}s"

# ---------------------------------------------------------------------------
# 5) Assertions — the evidence chain
# ---------------------------------------------------------------------------
EVIDENCE_DIR="$WORKSPACE_DIR/run-$RUN_ID/.agent_runtime/runs/run-$RUN_ID/vertex-$VERTEX_ID"

# 5a) control-plane record of the completed vertex
RESULT_JSON="$(psql_q "SELECT result_json FROM vertices WHERE id=$VERTEX_ID")"
[ -n "$RESULT_JSON" ] || fail "the completed vertex carries no result payload"
python3 - "$RESULT_JSON" "$BRANCH" <<'PY' || fail "unexpected vertex result in the control plane"
import json, sys
result, branch = json.loads(sys.argv[1]), sys.argv[2]
assert result["branch"] == branch, result["branch"]
assert result["verdict"]["passed"] is True, result["verdict"]
files = result["diff_files"]
assert files, "the harness produced an empty diff"
# the harness writes its own opencode.jsonc into the sandbox; it must never reach
# the diff, or it would be a paths_allow violation
assert "opencode.jsonc" not in files, f"harness config leaked into the diff: {files}"
print(f"control plane: branch={result['branch']} diff_files={files}")
PY

# 5b) the pushed branch really carries the new module and a test change
python3 - "$GITEA" "$TOKEN" "$ORG" "$REPO" "$BRANCH" <<'PY' \
  || fail "the pushed branch $BRANCH does not carry the expected change"
import base64, fnmatch, json, sys, urllib.request
gitea, token, owner, repo, branch = sys.argv[1:6]

def get(path):
    request = urllib.request.Request(
        gitea + path, headers={"Authorization": f"token {token}"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)

def tree(ref):
    # the git trees API resolves a commit sha only, never a branch name
    sha = get(f"/api/v1/repos/{owner}/{repo}/branches/{ref}")["commit"]["id"]
    data = get(f"/api/v1/repos/{owner}/{repo}/git/trees/{sha}?recursive=1")
    return {entry["path"] for entry in data["tree"]}

added = sorted(tree(branch) - tree("main"))
assert "src/wordtools/reverse.py" in added, f"reverse.py missing: {added}"
assert any(
    fnmatch.fnmatch(path, "tests/**") or fnmatch.fnmatch(path, "**/test_*.py")
    for path in added
), f"no test change on the branch: {added}"
data = get(f"/api/v1/repos/{owner}/{repo}/contents/src/wordtools/reverse.py?ref={branch}")
source = base64.b64decode(data["content"]).decode("utf-8")
assert "reverse_words" in source, source
print(f"gitea: {branch} adds {', '.join(added)}")
PY

# 5c) append-only session log with the harness event stream
[ -f "$EVIDENCE_DIR/session_log.ndjson" ] \
  || fail "no session log at $EVIDENCE_DIR/session_log.ndjson"
python3 - "$EVIDENCE_DIR/session_log.ndjson" <<'PY' || fail "the session log is not the expected evidence stream"
import json, sys
records = [
    json.loads(line)
    for line in open(sys.argv[1], encoding="utf-8")
    if line.strip()
]
types = [record["type"] for record in records]
for required in ("prompt", "harness_event", "done_when_check", "result"):
    assert required in types, f"missing {required!r} record: {sorted(set(types))}"
harness = [record for record in records if record["type"] == "harness_event"]
kinds = {record["payload"].get("type") for record in harness}
assert "session.execution.succeeded" in kinds, sorted(str(kind) for kind in kinds)
print(f"session log: {len(records)} records, {len(harness)} harness events, terminal=succeeded")
PY

# 5d) run manifest: pinned image, model, finalized with a passing verdict
[ -f "$EVIDENCE_DIR/manifest.json" ] || fail "no manifest at $EVIDENCE_DIR/manifest.json"
python3 - "$EVIDENCE_DIR/manifest.json" "$PINNED_IMAGE" "$MODEL" "$RUN_ID" "$VERTEX_ID" <<'PY' \
  || fail "the run manifest is not the expected evidence record"
import json, sys
path, image, model, run_id, vertex_id = sys.argv[1:6]
manifest = json.load(open(path, encoding="utf-8"))
assert manifest["image"] == image, manifest["image"]
assert manifest["model"] == model, manifest["model"]
assert manifest["run_id"] == int(run_id), manifest["run_id"]
assert manifest["vertex_id"] == int(vertex_id), manifest["vertex_id"]
assert manifest["ts_end"], manifest
assert manifest["diff_stat"] and "changed" in manifest["diff_stat"], manifest["diff_stat"]
assert manifest["verdict"]["passed"] is True, manifest.get("verdict")
print(f"manifest: image={manifest['image']} model={manifest['model']} "
      f"diff_stat={manifest['diff_stat']!r} verdict.passed=true")
PY

# 5e) sandbox teardown: pod, service and secret must be gone after the vertex
SANDBOX_NAME="agent-$RUN_ID-$VERTEX_ID"
for kind in pod service secret; do
  kubectl -n agents get "$kind/$SANDBOX_NAME" >/dev/null 2>&1 \
    && fail "$kind/$SANDBOX_NAME survived the vertex (teardown leak)"
done
echo "M4b flow: sandbox teardown clean (pod/service/secret removed)"

# 5f) NetworkPolicy: the agents namespace reaches Gitea and LiteLLM, but NOT
#     Postgres (cluster service or host NodePort). Positive control proves the
#     probe pod itself is fine before asserting the denials.
kubectl -n agents run m4b-netpol-probe --image=alpine:3.20 --restart=Never --command -- \
  sh -c 'wget -qO- -T 5 http://gitea-http.platform.svc.cluster.local:3000/api/v1/version >/dev/null || exit 11; \
    wget -qO- -T 8 http://litellm.platform.svc.cluster.local:4000/health/liveliness >/dev/null || exit 12; \
    nc -zv -w 3 postgres-platform-rw.platform.svc.cluster.local 5432 >/dev/null 2>&1 && exit 13; \
    nc -zv -w 3 192.168.5.2 15432 >/dev/null 2>&1 && exit 14; \
    echo NETPOL_OK'
kubectl -n agents wait --for=condition=Ready pod/m4b-netpol-probe --timeout=180s >/dev/null 2>&1 \
  || true
PROBE_OUT=""
for _ in 1 2 3 4 5 6; do
  PROBE_OUT="$(kubectl -n agents logs m4b-netpol-probe 2>/dev/null | tail -1)"
  [ "$PROBE_OUT" = "NETPOL_OK" ] && break
  sleep 2
done
kubectl -n agents delete pod m4b-netpol-probe --force --grace-period=0 >/dev/null 2>&1 || true
[ "$PROBE_OUT" = "NETPOL_OK" ] \
  || fail "NetworkPolicy probe failed (out: ${PROBE_OUT:-none}; 11=gitea 12=litellm 13=pg-cluster 14=pg-host)"
echo "M4b flow: NetworkPolicy verified — gitea+litellm reachable, Postgres denied"

echo "M4b flow: PASS — vertex $VERTEX_ID pushed to $BRANCH in $ORG/$REPO"
echo "M4b flow: evidence kept in $EVIDENCE_DIR (worker log: $WORKER_LOG)"
