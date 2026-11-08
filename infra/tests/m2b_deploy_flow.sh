#!/usr/bin/env bash
# Acceptance-тест M2b: demo-deploy в prod через Argo CD + метрики в Prometheus + логи в Loki.
# Предусловие: bootstrap прогнан на main-состоянии репо (section_demo применил Application).
# Использование: ./infra/tests/m2b_deploy_flow.sh
set -euo pipefail

NS=platform
fail() { echo "M2b deploy-flow: FAIL — $1"; exit 1; }

# 1) Argo CD: demo-prod Synced + Healthy
ARGO_PW="$(kubectl -n "$NS" get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)"
JJWT="$(curl -sf -X POST -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$ARGO_PW\"}" \
  http://localhost:30081/api/v1/session | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')"
[ -n "$JJWT" ] || fail "argocd login не удался"
SYNC_JSON="$(mktemp)"
curl -sf -H "Authorization: Bearer $JJWT" http://localhost:30081/api/v1/applications/demo-prod > "$SYNC_JSON" || fail "argocd api недоступна"
python3 - "$SYNC_JSON" <<'EOF' || fail "demo-prod не Synced/Healthy"
import json, sys
app = json.load(open(sys.argv[1]))
sync = app["status"]["sync"]["status"]
health = app["status"]["health"]["status"]
assert sync == "Synced" and health == "Healthy", f"sync={sync} health={health}"
EOF
echo "argocd: demo-prod Synced+Healthy"

# 2) podinfo Running в prod
kubectl -n prod wait --for=condition=Ready pod -l app=podinfo --timeout=120s >/dev/null \
  || fail "podinfo pod не Running в prod"
echo "prod: podinfo Running"

# 2b) сгенерировать трафик (podinfo логирует запросы; для шагов 3 и 4 нужны свежие метрики и логи)
kubectl -n prod port-forward svc/podinfo 19998:9898 >/tmp/m2b-pf-podinfo.log 2>&1 &
PF_APP=$!
sleep 3
for i in 1 2 3 4 5; do curl -sf http://localhost:19998/healthz >/dev/null || fail "podinfo не отвечает на трафик"; done
kill $PF_APP 2>/dev/null || true
echo "prod: traffic generated (5 requests)"

# 3) Prometheus: метрика podinfo_requests_total присутствует
kubectl -n "$NS" port-forward svc/kube-prometheus-stack-prometheus 9090:9090 >/tmp/m2b-pf-prom.log 2>&1 &
PF1=$!
sleep 4
trap 'kill $PF1 2>/dev/null || true' EXIT
PROM_JSON="$(mktemp)"
curl -sf "http://localhost:9090/api/v1/query?query=http_requests_total%7Bjob%3D%22podinfo%22%7D" > "$PROM_JSON" \
  || fail "prometheus api недоступна"
python3 - "$PROM_JSON" <<'EOF' || fail "метрика http_requests_total{job=podinfo} отсутствует в Prometheus"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["status"] == "success" and len(d["data"]["result"]) > 0, f"result={d['data']['result']}"
EOF
echo "prometheus: http_requests_total{job=podinfo} present"

# 4) Loki: логи из namespace prod
kubectl -n "$NS" port-forward svc/loki 3100:3100 >/tmp/m2b-pf-loki.log 2>&1 &
PF2=$!
sleep 4
Q="$(python3 -c 'import urllib.parse;print(urllib.parse.quote("{namespace=\"prod\"}"))')"
LOKI_JSON="$(mktemp)"
curl -sf "http://localhost:3100/loki/api/v1/query_range?query=$Q&limit=5&since=15m" > "$LOKI_JSON" \
  || fail "loki api недоступна"
python3 - "$LOKI_JSON" <<'EOF' || fail "логи prod-namespace отсутствуют в Loki"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["status"] == "success" and len(d["data"]["result"]) > 0, "no streams"
EOF
echo "loki: prod logs present"

kill $PF2 2>/dev/null || true
echo "M2b deploy-flow: PASS"
