#!/usr/bin/env bash
# Acceptance M2b: the demo service in prod through Argo CD + metrics in Prometheus +
# logs in Loki. Since M6 the demo is isv-product (Gitea swarm-lab/isv-product,
# deploy/ = Argo Rollouts canary; k6 drives a constant 10 rps).
# Precondition: `bootstrap.sh rollouts demo` applied.
# Usage: ./infra/tests/m2b_deploy_flow.sh
set -euo pipefail

NS=platform
fail() { echo "M2b deploy-flow: FAIL — $1"; exit 1; }

# 1) Argo CD: isv-product-prod Synced + Healthy
ARGO_PW="$(kubectl -n "$NS" get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)"
JJWT="$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$ARGO_PW\"}" \
  http://localhost:30081/api/v1/session | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')"
[ -n "$JJWT" ] || fail "argocd login failed"
SYNC_JSON="$(mktemp)"
curl -sf -H "Authorization: Bearer $JJWT" http://localhost:30081/api/v1/applications/isv-product-prod > "$SYNC_JSON" \
  || fail "argocd api unreachable"
python3 - "$SYNC_JSON" <<'EOF' || fail "isv-product-prod is not Synced/Healthy"
import json, sys
app = json.load(open(sys.argv[1]))
sync = app["status"]["sync"]["status"]
health = app["status"]["health"]["status"]
assert sync == "Synced" and health == "Healthy", f"sync={sync} health={health}"
EOF
echo "argocd: isv-product-prod Synced+Healthy"

# 2) the Rollout is Healthy and the k6 traffic generator runs
PHASE="$(kubectl -n prod get rollout isv-product -o jsonpath='{.status.phase}')"
[ "$PHASE" = "Healthy" ] || fail "rollout isv-product is $PHASE"
kubectl -n prod wait --for=condition=Available deploy/isv-product-k6 --timeout=120s >/dev/null \
  || fail "k6 traffic generator not available"
echo "prod: rollout isv-product Healthy, k6 running"

# 3) Prometheus: per-version request metrics of the service
kubectl -n "$NS" port-forward svc/kube-prometheus-stack-prometheus 9090:9090 >/tmp/m2b-pf-prom.log 2>&1 &
PF1=$!
sleep 4
trap 'kill $PF1 2>/dev/null || true' EXIT
PROM_JSON="$(mktemp)"
FOUND=0
for _ in 1 2 3 4 5 6; do
  curl -sf "http://localhost:9090/api/v1/query" \
    --data-urlencode 'query=sum by (rollouts_pod_template_hash) (rate(http_requests_total{namespace="prod",service="isv-product"}[1m]))' \
    > "$PROM_JSON" || fail "prometheus api unreachable"
  FOUND="$(python3 -c "import json;d=json.load(open('$PROM_JSON'));print(len(d['data']['result']))")"
  [ "$FOUND" -ge 1 ] && break
  sleep 10
done
[ "$FOUND" -ge 1 ] || fail "no http_requests_total for isv-product in Prometheus"
echo "prometheus: isv-product request rate present ($FOUND version series)"

# 4) Loki: logs from namespace prod (uvicorn access lines under k6 traffic)
kubectl -n "$NS" port-forward svc/loki 3100:3100 >/tmp/m2b-pf-loki.log 2>&1 &
PF2=$!
sleep 3
Q="$(python3 -c 'import urllib.parse;print(urllib.parse.quote("{namespace=\"prod\"}"))')"
LOKI_JSON="$(mktemp)"
FOUND=0
for _ in 1 2 3 4 5; do
  sleep 3
  if curl -sf "http://localhost:3100/loki/api/v1/query_range?query=$Q&limit=5&since=10m" > "$LOKI_JSON"; then
    FOUND="$(python3 -c "import json;d=json.load(open('$LOKI_JSON'));print(len(d.get('data',{}).get('result',[])))")"
    [ "$FOUND" -ge 1 ] && break
  fi
done
[ "$FOUND" -ge 1 ] || fail "prod-namespace logs missing in Loki (5 attempts)"
echo "loki: prod logs present ($FOUND streams)"

kill $PF2 2>/dev/null || true
echo "M2b deploy-flow: PASS"
