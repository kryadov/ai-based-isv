#!/usr/bin/env bash
# Acceptance M9 (polyglot dogfood): a TypeScript service through the SAME corridor.
#
#   1. `org-bootstrap onboard --stack typescript --template ts-service` creates
#      swarm-lab/ts-greeter (template code + typescript check profile +
#      protected main); the seed CI (npm ci, tsc, eslint, node:test, gitleaks,
#      semgrep) must be green;
#   2. an agent task (selfdev_flow.sh) adds GET /api/time with a node:test test;
#      done_when runs node in docker; PR → CI → bot review → HITL (auto-approved
#      here, as in m5) → merge;
#   3. the merged main passes `npm ci && npm test` in a clean node container.
#
# Usage: ./infra/scripts/toolbox.sh bash infra/tests/m9_flow.sh
set -euo pipefail

fail() { echo "M9 flow: FAIL — $1"; exit 1; }
cd "$(dirname "$0")/../.."

REPO=ts-greeter
GITEA=http://localhost:30080
NODE_IMAGE=node:22.12.0-alpine
DONE_CMD="docker run --rm -v \"\$PWD\":/w -w /w $NODE_IMAGE sh -c 'npm ci --no-audit --no-fund && npm test'"
# Under the repo path: the docker daemon sees the same paths as the toolbox only
# for the mounted repo, and step 3 mounts this dir into a node container.
mkdir -p .agent-runtime-workspace
TMP="$(mktemp -d -p "$PWD/.agent-runtime-workspace" m9.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

TOKEN="$(kubectl -n platform exec "$(kubectl -n platform get pod -l app.kubernetes.io/name=openbao -o name)" -- \
  env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$(cat infra/.vault-root-token)" bao kv get -field=token secret/gitea/admin)"
AUTH="Authorization: token $TOKEN"
API="$GITEA/api/v1/repos/swarm-lab/$REPO"

# --- 1) onboard from the template (fresh every run) ---------------------------------
curl -s -o /dev/null -X DELETE -H "$AUTH" "$API" || true
uv run org-bootstrap onboard --project "$REPO" --kind service --stack typescript \
  --template ts-service --reason "polyglot dogfood (M9)" | tail -1
SEED="$(curl -fsS -H "$AUTH" "$API/branches/main" | python3 -c 'import json,sys; print(json.load(sys.stdin)["commit"]["id"])')"
STATE=""
for _ in $(seq 1 80); do
  STATE="$(curl -fsS -H "$AUTH" "$API/commits/$SEED/status" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d["state"] if len(d.get("statuses") or []) >= 2 else "pending")')"
  case "$STATE" in success|failure|error) break ;; esac
  sleep 15
done
[ "$STATE" = "success" ] || fail "seed CI on $REPO is $STATE (typescript profile)"
echo "M9 flow: $REPO onboarded from ts-service; seed CI green (tsc, eslint, node:test, gitleaks, semgrep)"
git checkout -q -- infra/projects.yaml 2>/dev/null || true   # the registry entry is not part of the repo state

# --- 2) an agent change in the foreign stack ------------------------------------------
SELFDEV_REPO="$REPO" \
SELFDEV_TITLE="Add GET /api/time to ts-greeter" \
SELFDEV_BODY="In src/server.ts, add a route GET /api/time returning JSON {\"time\": <new Date().toISOString()>} (status 200). Put the logic in a new pure function nowIso(now: Date): string in src/time.ts and test it in test/time.test.ts with node:test and node:assert/strict (import from \"../src/time.js\"). TypeScript strict mode; ESM imports with .js extensions." \
SELFDEV_VERTEX="add GET /api/time (nowIso in src/time.ts) with a node:test test in test/time.test.ts" \
SELFDEV_DONE_CMD="$DONE_CMD" \
SELFDEV_PATHS="src/**,test/**" \
SELFDEV_AUTO_APPROVE_MERGE=1 \
  bash infra/tests/selfdev_flow.sh | tee "$TMP/selfdev.log" | grep -E "Selfdev flow: (run=|HITL|PASS|FAIL)" || true
grep -q "Selfdev flow: PASS" "$TMP/selfdev.log" || fail "the corridor run on the TypeScript repo did not pass"

# --- 3) merged main passes in a clean node container -----------------------------------
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="$AUTH" \
  git clone -q "$GITEA/swarm-lab/$REPO.git" "$TMP/main"
[ -f "$TMP/main/src/time.ts" ] || fail "merged main has no src/time.ts"
docker run --rm -v "$TMP/main":/src:ro "$NODE_IMAGE" sh -c \
  "cp -r /src /w && cd /w && npm ci --no-audit --no-fund >/dev/null 2>&1 && npx tsc --noEmit && npx eslint . && npm test 2>&1 | grep -E '^# (pass|fail)'" \
  | tee "$TMP/test.txt"
grep -q "^# fail 0" "$TMP/test.txt" || fail "merged main does not pass its tests"
echo "M9 flow: PASS — TypeScript service: template → agent change → typescript corridor → merged main green"
