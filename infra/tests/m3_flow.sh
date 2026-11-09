#!/usr/bin/env bash
# Acceptance M3: control plane против живого Postgres (CNPG database `swarm`).
# Использование: ./infra/tests/m3_flow.sh
set -euo pipefail
fail() { echo "M3 flow: FAIL — $1"; exit 1; }

NS=platform
cd "$(dirname "$0")/../.."

# 1) БД control plane существует (CNPG Database CR swarm-db)
kubectl apply -f infra/postgres/swarm-database.yaml >/dev/null
kubectl -n "$NS" wait --for=jsonpath='{.status.applied}'=true database/swarm-db --timeout=120s \
  || fail "CNPG database swarm не применилась"

# 2) port-forward Postgres → 15432
kubectl -n "$NS" port-forward svc/postgres-platform-rw 15432:5432 >/tmp/m3-pf-pg.log 2>&1 &
PF=$!
sleep 3
trap 'kill $PF 2>/dev/null || true' EXIT
# Wait until the port-forward is actually accepting connections
PGOK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
  (echo > /dev/tcp/127.0.0.1/15432) 2>/dev/null && PGOK=1 && break
  sleep 1
done
[ "$PGOK" = "1" ] || fail "port-forward к Postgres не поднялся"

# 3) интеграционный тест полного цикла против живого Postgres
PGPW="$(kubectl -n "$NS" get secret postgres-platform-app -o jsonpath='{.data.password}' | base64 -d)"
export M3_INTEGRATION_PG_URL="postgresql+asyncpg://gitea:${PGPW}@127.0.0.1:15432/swarm"
uv run pytest apps/agent_api/tests/test_integration_pg.py -q 2>&1 | tail -1 | grep -q "passed" \
  || fail "интеграционный тест на Postgres не прошёл"

echo "M3 flow: PASS (control plane on live Postgres)"
