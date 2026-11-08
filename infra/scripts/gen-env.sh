#!/usr/bin/env bash
# Генерирует infra/env.yaml — env-манифест окружения (артефакт VET): endpoint'ы и фактические версии.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NS=platform

gitea_chart="$(helm list -n "$NS" -o json | python3 -c 'import json,sys;r=[x for x in json.load(sys.stdin) if x["name"]=="gitea"];print(r[0]["chart"] if r else "unknown")')"
vault_chart="$(helm list -n "$NS" -o json | python3 -c 'import json,sys;r=[x for x in json.load(sys.stdin) if x["name"]=="vault"];print(r[0]["chart"] if r else "unknown")')"
cnpg_chart="$(helm list -n cnpg-system -o json | python3 -c 'import json,sys;r=[x for x in json.load(sys.stdin) if x["name"]=="cnpg"];print(r[0]["chart"] if r else "unknown")')"
op_chart="$(helm list -n "$NS" -o json | python3 -c 'import json,sys;r=[x for x in json.load(sys.stdin) if x["name"]=="openproject"];print(r[0]["chart"] if r else "unknown")')"
pg_image="$(kubectl -n "$NS" get cluster postgres-platform -o jsonpath='{.spec.imageName}')"
runner_image="$(kubectl -n "$NS" get deploy act-runner -o jsonpath='{.spec.template.spec.containers[0].image}')"
litellm_image="$(kubectl -n "$NS" get deploy litellm -o jsonpath='{.spec.template.spec.containers[0].image}')"
k3s_version="$(kubectl get node -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}')"

cat > "$ROOT/env.yaml" <<EOF
# L0 environment manifest — генерируется scripts/gen-env.sh, руками не редактировать.
# Артефакт VET (спека §7.6): версии и endpoint'ы фактического стенда.
generated_at: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cluster:
  name: swarm-lab
  kubernetes: $k3s_version
endpoints:
  gitea_http: http://localhost:30080
  gitea_ssh: localhost:30022
  postgres_internal: postgres-platform-rw.$NS.svc.cluster.local:5432
services:
  gitea:
    namespace: $NS
    release: $gitea_chart
  vault:
    namespace: $NS
    release: $vault_chart
    mode: dev
  postgres:
    namespace: $NS
    operator_release: $cnpg_chart
    image: $pg_image
  act_runner:
    namespace: $NS
    image: $runner_image
  openproject:
    namespace: $NS
    release: $op_chart
    note: password/basic-auth login broken in v17-slim (see TODO) — API auth via adapter pending M4
  litellm:
    namespace: $NS
    image: $litellm_image
    endpoint: http://localhost:30084/v1
    model_backend: host ollama (qwen2.5-coder:7b)
EOF
echo "env.yaml generated:"
cat "$ROOT/env.yaml"
