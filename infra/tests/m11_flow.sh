#!/usr/bin/env bash
# Acceptance M11: the platform runs as a standing service — no flow-side help.
#
# Precondition (host Git Bash): bash infra/scripts/platform.sh up [--ref <ref>]
#
#   1. the control plane is the Argo CD app platform-control (Synced/Healthy),
#      the panel answers on :30400, service endpoints refuse a call without the
#      bearer token, the daemon container is running;
#   2. browser e2e of the panel (Playwright): login refusal, every tab without
#      JS errors, EN/RU, a card decided from the Inbox;
#   3. a `swarm`-labelled issue in swarm-lab/isv-product becomes a run created
#      by the dispatcher (this script never creates runs or vertices); the
#      daemon is restarted once while the run is active and no second run
#      appears; merge cards are approved from the panel API as the human
#      operator (owner-authorized); the run is judged done, the verdict is
#      commented on the issue, the canary rollout ends Healthy.
#
# Usage: ./infra/scripts/toolbox.sh bash infra/tests/m11_flow.sh
set -euo pipefail

fail() { echo "M11 flow: FAIL — $1"; exit 1; }
cd "$(dirname "$0")/../.."

API=http://localhost:30400
GITEA=http://localhost:30080
REPO=swarm-lab/isv-product
DAEMON=swarm-platform-daemon
RUN_TIMEOUT_S="${M11_RUN_TIMEOUT_S:-7200}"
mkdir -p .agent-runtime-workspace
TMP="$(mktemp -d -p "$PWD/.agent-runtime-workspace" m11.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

POD="$(kubectl -n platform get pod -l app.kubernetes.io/name=openbao -o jsonpath='{.items[0].metadata.name}')"
bao() { kubectl -n platform exec "$POD" -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$(cat infra/.vault-root-token)" bao "$@"; }
SVC="$(bao kv get -field=token secret/platform/api)"
PANEL_PASSWORD="$(bao kv get -field=password secret/platform/ui)"
GTOK="$(bao kv get -field=token secret/gitea/admin)"
S=(-H "Authorization: Bearer $SVC")
G=(-H "Authorization: token $GTOK")
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

# --- 1) the standing platform ---------------------------------------------------
APP="$(kubectl -n platform get application platform-control -o jsonpath='{.status.sync.status}/{.status.health.status}')"
[ "$APP" = "Synced/Healthy" ] || fail "platform-control is $APP — run platform.sh up"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$API/healthz")" = "200" ] || fail ":30400/healthz"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$API/runs")" = "401" ] || fail "service API answered without the token"
[ "$(docker inspect -f '{{.State.Running}}' "$DAEMON" 2>/dev/null)" = "true" ] || fail "daemon $DAEMON not running"
echo "M11 flow: control plane Synced/Healthy, token enforced, daemon running"

# --- 2) browser e2e ----------------------------------------------------------------
CARD="$(curl -fsS "${S[@]}" -H 'Content-Type: application/json' \
  -d '{"kind":"merge","subject":"m11 e2e: panel decision","context":{"summary":"browser e2e card"}}' \
  "$API/hitl" | json 'd["request_id"]')"
PANEL_URL="$API" PANEL_USER=owner PANEL_PASSWORD="$PANEL_PASSWORD" PANEL_CARD_ID="$CARD" \
  bash infra/tests/e2e/run.sh > "$TMP/e2e.log" 2>&1 || { tail -30 "$TMP/e2e.log"; fail "browser e2e"; }
[ "$(curl -fsS "${S[@]}" "$API/hitl/$CARD" | json 'd["status"]')" = "approved" ] || fail "e2e card not approved"
echo "M11 flow: browser e2e PASS (card #$CARD approved in Chromium)"

# --- 3) a tracker issue, picked up by the standing dispatcher ------------------------
TITLE="${M11_TITLE:-Add GET /api/ping returning {\"pong\": \"ok\"}}"
BODY="${M11_BODY:-In src/isv_product/main.py add an endpoint GET /api/ping that returns the JSON object {\"pong\": \"ok\"}, next to the existing /api/version endpoint. Add a pytest test in tests/test_ping.py using the same test-client pattern as the existing tests. Do not change anything else.}"
LABEL_ID="$(curl -fsS "${G[@]}" "$GITEA/api/v1/repos/$REPO/labels" | json 'next(l["id"] for l in d if l["name"]=="swarm")')"
python3 - "$TITLE" "$BODY" "$LABEL_ID" > "$TMP/issue.json" <<'PY'
import json, sys
print(json.dumps({"title": sys.argv[1], "body": sys.argv[2], "labels": [int(sys.argv[3])]}))
PY
NUM="$(curl -fsS "${G[@]}" -H 'Content-Type: application/json' -X POST "$GITEA/api/v1/repos/$REPO/issues" \
  --data @"$TMP/issue.json" | json 'd["number"]')"
REF="$REPO#$NUM"
echo "M11 flow: filed $REF (label swarm) — waiting for the dispatcher"
RUN=""
for _ in $(seq 1 40); do
  RUN="$(curl -fsS "${S[@]}" "$API/runs?task_ref=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$REF")" \
    | json '",".join(str(r["id"]) + ":" + str(r["vertex_count"]) for r in d["runs"])')"
  case "$RUN" in *:[1-9]*) break ;; esac
  sleep 15
done
case "$RUN" in *,*) fail "more than one run for $REF ($RUN)" ;; *:[1-9]*) ;; *) fail "no planned run for $REF ($RUN)" ;; esac
RUN_ID="${RUN%%:*}"
echo "M11 flow: dispatcher created run $RUN_ID (${RUN#*:} vertex(es)) for $REF"

docker restart "$DAEMON" >/dev/null
echo "M11 flow: daemon restarted while run $RUN_ID is active"

# --- the corridor: approve this repo's merge cards from the panel as the operator ---
JAR="$TMP/cookies"
curl -fsS -o /dev/null -c "$JAR" --data-urlencode "username=owner" --data-urlencode "password=$PANEL_PASSWORD" "$API/ui/login" || true
grep -q swarm_session "$JAR" || fail "panel login failed"
START="$(date +%s)"
STATUS=pending
while [ $(( $(date +%s) - START )) -lt "$RUN_TIMEOUT_S" ]; do
  for id in $(curl -fsS -b "$JAR" "$API/ui/api/inbox" \
      | json '" ".join(str(c["id"]) for c in d["cards"] if c["kind"]=="merge" and "/swarm-lab/isv-product/pulls/" in str((c.get("context") or {}).get("pr_url","")))'); do
    curl -fsS -o /dev/null -b "$JAR" -H 'Content-Type: application/json' \
      -d '{"action":"approve","note":"m11 acceptance: operator approval under the owner pre-authorization"}' \
      "$API/ui/api/hitl/$id/decision" && echo "M11 flow: merge card #$id approved from the panel"
  done
  STATUS="$(curl -fsS "${S[@]}" "$API/runs?task_ref=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$REF")" | json 'd["runs"][0]["status"]')"
  [ "$STATUS" = "pending" ] || break
  sleep 30
done
COUNT="$(curl -fsS "${S[@]}" "$API/runs?task_ref=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$REF")" | json 'len(d["runs"])')"
[ "$COUNT" = "1" ] || fail "$COUNT runs for $REF after the restart"
[ "$STATUS" = "done" ] || { docker logs --tail 40 "$DAEMON"; fail "run $RUN_ID is $STATUS"; }
curl -fsS "${G[@]}" "$GITEA/api/v1/repos/$REPO/issues/$NUM/comments" \
  | json '"\n".join(c["body"] for c in d)' | grep -q "judged \*\*done\*\*" || fail "no verdict comment on $REF"
PHASE="$(kubectl -n prod get rollout isv-product -o jsonpath='{.status.phase}')"
[ "$PHASE" = "Healthy" ] || fail "rollout isv-product is $PHASE"
echo "M11 flow: run $RUN_ID judged done, verdict commented on $REF, rollout Healthy"
echo "M11 flow: PASS"
