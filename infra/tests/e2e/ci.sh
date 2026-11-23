#!/usr/bin/env bash
# Browser e2e of the panel in CI (M12): a local agent_api on SQLite with a test
# panel user, one seeded HITL card, then test_panel.py in a real Chromium.
#
#   bash infra/tests/e2e/ci.sh        (repo root; needs uv; Chromium is installed
#                                      by `playwright install` — see ci.yml)
#
# Everything is throwaway: the database and the panel password live in a temp
# dir and die with the run. The service API stays open (no token) because the
# server binds 127.0.0.1 only.
set -euo pipefail
cd "$(dirname "$0")/../../.."

PORT="${E2E_PORT:-18400}"
WORK="$(mktemp -d)"
PW_DEPS=(--with playwright==1.49.0 --with pytest-playwright==0.6.2)
cleanup() {
  [ -n "${API_PID:-}" ] && kill "$API_PID" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

PANEL_USER=e2e
PANEL_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
HASH="$(printf '%s' "$PANEL_PASSWORD" | sha256sum | cut -d' ' -f1)"

AGENT_API_DATABASE_URL="sqlite+aiosqlite:///$WORK/e2e.db" \
AGENT_API_UI_USERS="$PANEL_USER:$HASH:admin" \
AGENT_API_SESSION_SECRET="$(head -c 32 /dev/urandom | base64)" \
  uv run --all-packages uvicorn --factory agent_api.main:create_app \
    --host 127.0.0.1 --port "$PORT" >"$WORK/api.log" 2>&1 &
API_PID=$!

URL="http://127.0.0.1:$PORT"
for _ in $(seq 1 60); do
  curl -sf -o /dev/null "$URL/healthz" && break
  sleep 1
done
curl -sf -o /dev/null "$URL/healthz" || { cat "$WORK/api.log"; echo "e2e: agent_api did not start"; exit 1; }

CARD_ID="$(curl -sf -X POST "$URL/hitl" -H 'Content-Type: application/json' \
  -d '{"kind":"merge","subject":"e2e: merge swarm-lab/demo#1","context":{"pr_url":"http://gitea.test/pulls/1"}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["request_id"])')"

export PANEL_URL="$URL" PANEL_USER PANEL_PASSWORD PANEL_CARD_ID="$CARD_ID"
uv run --all-packages "${PW_DEPS[@]}" python -m pytest -q -p no:cacheprovider \
  infra/tests/e2e/test_panel.py --browser chromium
echo "e2e: panel PASS (card #$CARD_ID decided in Chromium)"
