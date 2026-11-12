#!/usr/bin/env bash
# Acceptance M2c: трекер (Gitea issues, v0) → TaskSource-поллер → тест-endpoint; LiteLLM completion + лог.
# Использование: ./infra/tests/m2c_flow.sh
set -euo pipefail

NS=platform
GITEA=http://localhost:30080
OWNER=swarm-admin
REPO=m2c-selftest
TOKEN="$(kubectl -n "$NS" exec "$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=openbao -o jsonpath='{.items[0].metadata.name}')" -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$(cat infra/.vault-root-token)" bao kv get -field=token secret/gitea/admin)"
[ -n "$TOKEN" ] || fail() { echo "M2c flow: FAIL — $1"; exit 1; }
AUTH="Authorization: token $TOKEN"
fail() { echo "M2c flow: FAIL — $1"; exit 1; }

rm -f /tmp/m2c-checkpoint /tmp/m2c-last-payload.json

# 1) пересоздать selftest-репо (идемпотентность теста)
curl -s -o /dev/null -X DELETE -H "$AUTH" "$GITEA/api/v1/repos/$OWNER/$REPO" || true
code="$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" -H 'Content-Type: application/json' \
  -X POST "$GITEA/api/v1/user/repos" -d "{\"name\":\"$REPO\",\"private\":true,\"auto_init\":true}")"
[ "$code" = "201" ] || fail "создание репо ($code)"

# 2) создать задачу (issue) через API трекера
code="$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" -H 'Content-Type: application/json' \
  -X POST "$GITEA/api/v1/repos/$OWNER/$REPO/issues" \
  -d '{"title":"M2c: deliver me to the platform","body":"acceptance issue"}')"
[ "$code" = "201" ] || fail "создание issue ($code)"

# 3) тест-endpoint + поллер (первый прогон — доставка)
python3 infra/tests/test_endpoint.py >/dev/null 2>&1 &
EP=$!
sleep 2
trap 'kill $EP 2>/dev/null || true' EXIT
export GITEA_URL="$GITEA" GITEA_TOKEN="$TOKEN" OWNER REPO
export ENDPOINT=http://127.0.0.1:8999 CHECKPOINT=/tmp/m2c-checkpoint
OUT1="$(python3 infra/tests/task_source_poller.py)"
echo "$OUT1" | grep -q "delivered 1" || fail "поллер не доставил задачу ($OUT1)"
[ -f /tmp/m2c-last-payload.json ] || fail "payload не записан"
python3 - <<'EOF' || fail "payload не содержит issue"
import json
d = json.load(open("/tmp/m2c-last-payload.json"))
assert d["issues"] and d["issues"][0]["title"].startswith("M2c:"), d
EOF
H1="$(cksum < /tmp/m2c-last-payload.json)"

# 4) второй прогон — дедупликация (checkpoint): 0 доставок, payload не изменился
OUT2="$(python3 infra/tests/task_source_poller.py)"
echo "$OUT2" | grep -q "delivered 0" || fail "поллер повторно доставил те же задачи ($OUT2)"
H2="$(cksum < /tmp/m2c-last-payload.json)"
[ "$H1" = "$H2" ] || fail "payload изменился на дедуп-прогоне"

# 5) LiteLLM: completion через шлюз
MK="$(kubectl -n "$NS" exec "$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=openbao -o jsonpath='{.items[0].metadata.name}')" -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$(cat infra/.vault-root-token)" bao kv get -field=master-key secret/litellm/master)"
LIT="$(mktemp)"
curl -sf -m 120 -X POST http://localhost:30084/v1/chat/completions \
  -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' \
  -d '{"model":"qwen-coder","messages":[{"role":"user","content":"Reply with exactly: GATEWAY-OK"}],"max_tokens":16}' > "$LIT" 2>/dev/null \
  || fail "litellm недоступен"
python3 - "$LIT" <<'EOF' || fail "пустой completion от модели"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["choices"][0]["message"]["content"].strip(), d
EOF
grep -q "chat/completions" <(kubectl -n "$NS" logs deploy/litellm --since=5m) || fail "вызов не залогирован litellm"

# 6) cleanup
curl -s -o /dev/null -X DELETE -H "$AUTH" "$GITEA/api/v1/repos/$OWNER/$REPO"

echo "M2c flow: PASS (tasksource + gateway)"
