#!/usr/bin/env bash
# Acceptance M3: control plane против живого Postgres (CNPG database `swarm`).
# Постгрес доступен с хоста через NodePort 15432 (infra/postgres/nodeport.yaml,
# k3d маппинг 15432:15432) — port-forward не используется.
# Использование: ./infra/tests/m3_flow.sh
set -euo pipefail
fail() { echo "M3 flow: FAIL — $1"; exit 1; }

NS=platform
cd "$(dirname "$0")/../.."

# 1) БД control plane существует (CNPG Database CR swarm-db)
kubectl apply -f infra/postgres/swarm-database.yaml >/dev/null
kubectl -n "$NS" wait --for=jsonpath='{.status.applied}'=true database/swarm-db --timeout=120s \
  || fail "CNPG database swarm не применилась"
kubectl -n "$NS" apply -f infra/postgres/nodeport.yaml >/dev/null

# 2) NodePort доступен с хоста
PGOK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
  PGPW="$(kubectl -n "$NS" get secret postgres-platform-app -o jsonpath='{.data.password}' | base64 -d)"
  if PGPASSWORD="$PGPW" psql -h localhost -p 15432 -U gitea -d swarm -tc "SELECT 1" >/dev/null 2>&1; then
    PGOK=1
    break
  fi
  sleep 2
done
[ "$PGOK" = "1" ] || fail "postgres NodePort 15432 недоступен"

# 3) интеграционный тест полного цикла против живого Postgres
PGPW="$(kubectl -n "$NS" get secret postgres-platform-app -o jsonpath='{.data.password}' | base64 -d)"
export M3_INTEGRATION_PG_URL="postgresql+asyncpg://gitea:${PGPW}@127.0.0.1:15432/swarm"
uv run pytest apps/agent_api/tests/test_integration_pg.py -q 2>&1 | tail -1 | grep -q "passed" \
  || fail "интеграционный тест на Postgres не прошёл"

echo "M3 flow: PASS (control plane on live Postgres)"
