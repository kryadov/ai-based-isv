#!/usr/bin/env bash
# Acceptance M10: the organization panel + Automations.
#
#   1. human login: unauthenticated → login page, bad password → 401, good → the
#      app shell with every tab; /ui/api requires the session;
#   2. an Inbox merge card is decided from the panel API, recorded under the
#      human's name; a second decision is refused (409);
#   3. vuln-scan requested from the panel on swarm-lab/vuln-demo (a repo with a
#      known `subprocess(..., shell=True)` finding): the engine scans (semgrep +
#      gitleaks), files an Inbox card; approving it from the panel makes the
#      engine create the fix task in the repo's tracker; nothing before approval.
#
# Usage: ./infra/scripts/toolbox.sh bash infra/tests/m10_flow.sh
set -euo pipefail

fail() { echo "M10 flow: FAIL — $1"; exit 1; }
cd "$(dirname "$0")/../.."

GITEA=http://localhost:30080
PORT="${M10_API_PORT:-8474}"
API="http://127.0.0.1:$PORT"
mkdir -p .agent-runtime-workspace
TMP="$(mktemp -d -p "$PWD/.agent-runtime-workspace" m10.XXXXXX)"
API_PID=""
cleanup() { [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

TOKEN="$(kubectl -n platform exec "$(kubectl -n platform get pod -l app.kubernetes.io/name=openbao -o name)" -- \
  env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$(cat infra/.vault-root-token)" bao kv get -field=token secret/gitea/admin)"
AUTH="Authorization: token $TOKEN"

# --- the demo repo with a known finding --------------------------------------------
curl -s -o /dev/null -X DELETE -H "$AUTH" "$GITEA/api/v1/repos/swarm-lab/vuln-demo" || true
curl -fsS -o /dev/null -H "$AUTH" -H 'Content-Type: application/json' -X POST "$GITEA/api/v1/orgs/swarm-lab/repos" \
  -d '{"name":"vuln-demo","private":true,"auto_init":true,"default_branch":"main"}' || fail "could not create vuln-demo"
APP_B64="$(printf 'import subprocess\n\n\ndef run(cmd: str) -> int:\n    return subprocess.call(cmd, shell=True)\n' | base64 -w0)"
curl -fsS -o /dev/null -H "$AUTH" -H 'Content-Type: application/json' -X POST \
  "$GITEA/api/v1/repos/swarm-lab/vuln-demo/contents/app.py" \
  -d "{\"message\":\"demo: a shell=True call\",\"content\":\"$APP_B64\"}" || fail "could not seed vuln-demo"
echo "M10 flow: swarm-lab/vuln-demo seeded with a shell=True call"

# --- the control plane with one human user ------------------------------------------
mkdir -p "$TMP/automations"
sed 's#^scope: .*#scope: [swarm-lab/vuln-demo]#' infra/automations/vuln-scan.yaml > "$TMP/automations/vuln-scan.yaml"
PW_HASH="$(printf '%s' 'm10-pass' | sha256sum | cut -d' ' -f1)"
PGPW="$(kubectl -n platform get secret postgres-platform-app -o jsonpath='{.data.password}' | base64 -d)"
AGENT_API_DATABASE_URL="postgresql+asyncpg://gitea:$PGPW@localhost:15432/swarm" \
AGENT_API_UI_USERS="operator:$PW_HASH:admin" \
AGENT_API_SESSION_SECRET="$(openssl rand -hex 24)" \
AGENT_API_AUTOMATIONS_DIR="$TMP/automations" \
  uv run uvicorn --factory agent_api.main:create_app --host 127.0.0.1 --port "$PORT" > "$TMP/api.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 30); do curl -sf -m 2 -o /dev/null "$API/healthz" && break; sleep 1; done
curl -sf -o /dev/null "$API/healthz" || { tail -20 "$TMP/api.log"; fail "agent_api did not start"; }

# --- 1) human login ------------------------------------------------------------------
JAR="$TMP/cookies"
curl -fsS "$API/ui" | grep -q 'id="login-form"' || fail "unauthenticated /ui is not the login page"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$API/ui/api/me")" = "401" ] || fail "/ui/api/me without a session"
[ "$(curl -s -o /dev/null -w '%{http_code}' -d 'username=operator&password=wrong' "$API/ui/login")" = "401" ] \
  || fail "a bad password was not refused"
curl -fsS -o /dev/null -c "$JAR" -d 'username=operator&password=m10-pass' "$API/ui/login" || true
grep -q swarm_session "$JAR" || fail "login did not set the session cookie"
SHELL_HTML="$(curl -fsS -b "$JAR" "$API/ui")"
for tab in inbox projects swarm pipeline automations metrics memory settings; do
  echo "$SHELL_HTML" | grep -q "data-tab=\"$tab\"" || fail "shell lacks tab $tab"
done
curl -fsS -b "$JAR" "$API/ui/api/me" | grep -q '"user":"operator"' || fail "/ui/api/me is not the operator"
echo "M10 flow: login works; the shell has all 8 tabs"

# --- 2) an Inbox decision from the panel ---------------------------------------------
HID="$(curl -fsS -H 'Content-Type: application/json' -d '{"kind":"merge","subject":"m10 demo PR","context":{}}' "$API/hitl" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["request_id"])')"
curl -fsS -b "$JAR" -H 'Content-Type: application/json' -d '{"action":"approve","note":"m10"}' \
  "$API/ui/api/hitl/$HID/decision" | grep -q '"decided_by":"operator"' || fail "the panel decision was not recorded as the operator"
[ "$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" -H 'Content-Type: application/json' -d '{"action":"reject"}' \
  "$API/ui/api/hitl/$HID/decision")" = "409" ] || fail "a second decision was not refused"
echo "M10 flow: merge card #$HID approved from the panel by operator (second decision refused)"

# --- 3) vuln-scan: requested in the panel → card → approval → fix task ----------------
curl -fsS -o /dev/null -b "$JAR" -X POST "$API/ui/api/automations/vuln-scan/run" || fail "automation request failed"
export AGENT_RUNTIME_API_URL="$API" AUTOMATIONS_GITEA_TOKEN="$TOKEN" AUTOMATIONS_WORKDIR="$TMP/work"
uv run python -m agent_runtime.automations poll --once | tee "$TMP/poll1.txt"
RUN_JSON="$(curl -fsS "$API/automations/runs?name=vuln-scan&limit=1")"
read -r RUN_ID RUN_STATUS CARD NFIND < <(python3 -c '
import json, sys
r = json.loads(sys.argv[1])["runs"][0]
print(r["id"], r["status"], r["hitl_id"], len(r["report"].get("findings", [])))' "$RUN_JSON")
[ "$RUN_STATUS" = "awaiting_approval" ] || fail "run $RUN_ID is $RUN_STATUS, expected awaiting_approval ($RUN_JSON)"
echo "M10 flow: vuln-scan run $RUN_ID found $NFIND finding(s); Inbox card #$CARD"
ISSUES_BEFORE="$(curl -fsS -H "$AUTH" "$GITEA/api/v1/repos/swarm-lab/vuln-demo/issues?state=all&type=issues" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')"
[ "$ISSUES_BEFORE" = "0" ] || fail "issues exist before the human approved"
curl -fsS -b "$JAR" "$API/ui/api/inbox" | grep -q '"kind":"automation"' || fail "no automation card in the Inbox"
curl -fsS -o /dev/null -b "$JAR" -H 'Content-Type: application/json' -d '{"action":"approve"}' \
  "$API/ui/api/hitl/$CARD/decision" || fail "approving the automation card failed"
uv run python -m agent_runtime.automations poll --once | tee "$TMP/poll2.txt"
DONE="$(curl -fsS "$API/automations/runs?name=vuln-scan&limit=1" | python3 -c '
import json, sys
r = json.load(sys.stdin)["runs"][0]
print(r["status"], len(r["created_issues"]))')"
[ "$DONE" = "done 1" ] || fail "the run did not create exactly one fix task ($DONE)"
TITLE="$(curl -fsS -H "$AUTH" "$GITEA/api/v1/repos/swarm-lab/vuln-demo/issues?state=open&type=issues" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["title"])')"
case "$TITLE" in "[vuln-scan]"*) ;; *) fail "unexpected issue title: $TITLE";; esac
echo "M10 flow: approved card → fix task \"$TITLE\" in swarm-lab/vuln-demo"
echo "M10 flow: PASS"
