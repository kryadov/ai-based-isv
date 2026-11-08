#!/usr/bin/env bash
# Acceptance-тест M2a: PR-flow целевого репо с branch protection (критерий инкремента, спека §10 M2a).
# Использование: ./infra/tests/m2a_pr_flow.sh
set -euo pipefail

NS=platform
GITEA=http://localhost:30080
OWNER=swarm-admin
REPO=m2a-selftest
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "M2a PR-flow: FAIL — $1"; exit 1; }

# 1) токен админа из Vault
TOKEN="$(kubectl -n "$NS" exec "$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=openbao -o jsonpath='{.items[0].metadata.name}')" -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$(cat infra/.vault-root-token)" bao kv get -field=token secret/gitea/admin)"
[ -n "$TOKEN" ] || fail "нет админ-токена в Vault"
AUTH="Authorization: token $TOKEN"
code="$(curl -s -o /dev/null -w '%{http_code}' -H "$AUTH" "$GITEA/api/v1/user")"
[ "$code" = "200" ] || fail "gitea api недоступна (user=$code)"

# 2) пересоздать selftest-репо (идемпотентность теста)
curl -s -o /dev/null -X DELETE -H "$AUTH" "$GITEA/api/v1/repos/$OWNER/$REPO" || true
code="$(curl -s -o /tmp/m2a-repo.json -w '%{http_code}' -H "$AUTH" -H 'Content-Type: application/json' \
  -X POST "$GITEA/api/v1/user/repos" \
  -d "{\"name\":\"$REPO\",\"private\":true,\"auto_init\":true,\"default_branch\":\"main\"}")"
[ "$code" = "201" ] || fail "создание репо ($code): $(cat /tmp/m2a-repo.json)"

# 3) branch protection на main: push запрещён, требуется 1 approve
code="$(curl -s -o /tmp/m2a-bp.json -w '%{http_code}' -H "$AUTH" -H 'Content-Type: application/json' \
  -X POST "$GITEA/api/v1/repos/$OWNER/$REPO/branch_protections" \
  -d '{"branch_name":"main","enable_push":false,"required_approvals":1,"enable_approvals_whitelist":false}')"
[ "$code" = "201" ] || fail "создание branch protection ($code): $(cat /tmp/m2a-bp.json)"

# 4) push начального состояния + фича-ветка с изменением
cd "$WORK"
git init -q -b main selftest && cd selftest
git config user.email m2a-test@lab.local && git config user.name m2a-test
git remote add origin "http://swarm-admin:$TOKEN@localhost:30080/$OWNER/$REPO.git"
git pull -q origin main
git checkout -q -b feat/test
echo "m2a change" >> feature.txt && git add feature.txt && git commit -qm "m2a: feature change"
git push -q origin feat/test

# 5) открыть PR
code="$(curl -s -o /tmp/m2a-pr.json -w '%{http_code}' -H "$AUTH" -H 'Content-Type: application/json' \
  -X POST "$GITEA/api/v1/repos/$OWNER/$REPO/pulls" \
  -d '{"title":"m2a: test PR","head":"feat/test","base":"main"}')"
[ "$code" = "201" ] || fail "создание PR ($code): $(cat /tmp/m2a-pr.json)"
PR_INDEX="$(python3 -c 'import json;print(json.load(open("/tmp/m2a-pr.json"))["number"])')"

# 6) merge без approve должен быть ЗАПРЕЩЁН (branch protection работает)
code="$(curl -s -o /tmp/m2a-merge.json -w '%{http_code}' -H "$AUTH" -H 'Content-Type: application/json' \
  -X POST "$GITEA/api/v1/repos/$OWNER/$REPO/pulls/$PR_INDEX/merge" \
  -d '{"Do":"merge"}')"
case "$code" in
  405|422|403) echo "merge blocked as expected (HTTP $code): $(head -c 160 /tmp/m2a-merge.json)" ;;
  200) fail "PR смержился БЕЗ approve — branch protection не работает!" ;;
  *) fail "неожиданный ответ на merge ($code): $(cat /tmp/m2a-merge.json)" ;;
esac
state="$(curl -sf -H "$AUTH" "$GITEA/api/v1/repos/$OWNER/$REPO/pulls/$PR_INDEX" | python3 -c 'import json,sys;print(json.load(sys.stdin)["state"])')"
[ "$state" = "open" ] || fail "PR не открыт после заблокированного merge (state=$state)"

# 7) защита на месте
n="$(curl -sf -H "$AUTH" "$GITEA/api/v1/repos/$OWNER/$REPO/branch_protections" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
[ "$n" -ge 1 ] || fail "branch protection отсутствует"

# 8) cleanup (репо с непринятым PR удаляется целиком)
curl -s -o /dev/null -X DELETE -H "$AUTH" "$GITEA/api/v1/repos/$OWNER/$REPO"

echo "M2a PR-flow: PASS"
