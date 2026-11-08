#!/usr/bin/env bash
# Acceptance M2d: бутстреппер-CLI end-to-end на живом стенде.
# Использование: ./infra/tests/m2d_flow.sh
set -euo pipefail
fail() { echo "M2d flow: FAIL — $1"; exit 1; }

cd "$(dirname "$0")/../.."

# 1) prerequisites
uv run org-bootstrap check || fail "prerequisites не на месте"

# 2) профиль (идемпотентно: force перезапись)
uv run org-bootstrap init --force || fail "init не прошёл"
[ -f infra/env.local.yaml ] || fail "профиль infra/env.local.yaml не создан"

# 3) apply всех секций на живом стенде (идемпотентно)
uv run org-bootstrap apply || fail "apply завершился с ошибкой"

# 4) seed-demo
uv run org-bootstrap seed-demo || fail "seed-demo не прошёл"

# 5) demo-репо существует и README на месте
GTOKEN="$(kubectl -n platform exec "$(kubectl -n platform get pod -l app.kubernetes.io/name=openbao -o jsonpath='{.items[0].metadata.name}')" -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$(cat infra/.vault-root-token)" bao kv get -field=token secret/gitea/admin)"
code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: token $GTOKEN" http://localhost:30080/api/v1/repos/swarm-admin/isv-product)"
[ "$code" = "200" ] || fail "demo-репо недоступно ($code)"
code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: token $GTOKEN" http://localhost:30080/api/v1/repos/swarm-admin/isv-product/contents/README.md)"
[ "$code" = "200" ] || fail "README.md в demo-репо отсутствует ($code)"

# 6) env.yaml консистентен (сгенерирован apply-цепочкой)
[ -f infra/env.yaml ] || fail "infra/env.yaml отсутствует"

echo "M2d flow: PASS (check + init + apply + seed-demo + demo repo verified)"
