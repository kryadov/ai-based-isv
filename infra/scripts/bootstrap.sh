#!/usr/bin/env bash
# Секции L0; вызываются верхнеуровневым infra/bootstrap.sh (все секции идемпотентны).
# Порядок полного прогона: vault → cnpg → gitea (внешняя БД, если CNPG поднят) → runner.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NS=platform

# CLI-хелперы Vault через exec в pod
vcli() { kubectl -n "$NS" exec vault-0 -- env VAULT_ADDR=http://127.0.0.1:8200 vault "$@"; }

section_vault() {
  helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null 2>&1 || true
  helm repo update hashicorp >/dev/null
  helm upgrade --install vault hashicorp/vault \
    --namespace "$NS" --create-namespace \
    -f "$ROOT/vault/values.yaml" --wait --timeout 5m
  kubectl -n "$NS" wait --for=condition=Ready pod -l app.kubernetes.io/name=vault --timeout=300s
  kubectl -n "$NS" exec vault-0 -- vault status -address=http://127.0.0.1:8200 | grep -E "Sealed|Initialized"
}

section_cnpg() {
  helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null 2>&1 || true
  helm repo update cnpg >/dev/null
  helm upgrade --install cnpg cnpg/cloudnative-pg \
    --namespace cnpg-system --create-namespace --wait --timeout 5m
  kubectl apply -f "$ROOT/postgres/cluster.yaml"
  kubectl -n "$NS" wait --for=condition=Ready cluster/postgres-platform --timeout=600s
  echo "cnpg: postgres-platform Ready (pgvector standard image)"
}

# Гарантирует валидный админ-токен в Vault: проверяет текущий, при 401 создаёт новый через basic-auth.
ensure_admin_token() {
  local pw old tok code
  pw="$(vcli kv get -field=password secret/gitea/admin 2>/dev/null || true)"
  old="$(vcli kv get -field=token secret/gitea/admin 2>/dev/null || true)"
  if [ -n "$old" ]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: token $old" http://localhost:30080/api/v1/user || true)"
    [ "$code" = "200" ] && { echo "gitea: existing admin token valid"; return 0; }
  fi
  local name="bootstrap-$(date +%s)"
  code="$(curl -s -o /tmp/gitea-token.json -w '%{http_code}' \
    -u "swarm-admin:$pw" -X POST \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$name\",\"scopes\":[\"all\"]}" \
    http://localhost:30080/api/v1/users/swarm-admin/tokens)"
  [ "$code" = "201" ] || { echo "gitea: token create failed ($code): $(cat /tmp/gitea-token.json)"; exit 1; }
  tok="$(python3 -c 'import json;print(json.load(open("/tmp/gitea-token.json"))["sha1"])')"
  [ -n "$tok" ] || { echo "gitea: empty token sha1"; exit 1; }
  vcli kv put secret/gitea/admin username=swarm-admin "password=$pw" "token=$tok" >/dev/null
  echo "gitea: admin token stored in vault"
}

section_gitea() {
  helm repo add gitea https://dl.gitea.com/charts/ >/dev/null 2>&1 || true
  helm repo update gitea >/dev/null
  local pw dbpw
  pw="$(vcli kv get -field=password secret/gitea/admin 2>/dev/null || true)"
  [ -n "$pw" ] || { pw="$(openssl rand -hex 16)"; vcli kv put secret/gitea/admin username=swarm-admin "password=$pw" >/dev/null; }
  local extvals=()
  if kubectl -n "$NS" get secret postgres-platform-app >/dev/null 2>&1; then
    dbpw="$(kubectl -n "$NS" get secret postgres-platform-app -o jsonpath='{.data.password}' | base64 -d)"
    extvals=(--values "$ROOT/gitea/values-external-db.yaml" --set "gitea.config.database.PASSWD=$dbpw")
  fi
  helm upgrade --install gitea gitea/gitea \
    --namespace "$NS" \
    --set gitea.admin.username=swarm-admin \
    --set "gitea.admin.password=$pw" \
    --values "$ROOT/gitea/values.yaml" \
    "${extvals[@]}" --wait --timeout 10m
  kubectl -n "$NS" wait --for=condition=Ready pod -l app.kubernetes.io/name=gitea --timeout=300s 2>/dev/null || true
  ensure_admin_token
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:30080/api/v1/version || true)"
  [ "$code" = "200" ] || { echo "gitea: api not reachable ($code)"; exit 1; }
}

section_runner() {
  # Регистрационный токен runner'а из Gitea admin API → secret → apply.
  local rtok n
  ensure_admin_token
  local atok; atok="$(vcli kv get -field=token secret/gitea/admin)"
  rtok="$(curl -sf -X POST -H "Authorization: token $atok" http://localhost:30080/api/v1/admin/actions/runners/registration-token | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')"
  kubectl -n "$NS" create secret generic act-runner-registration \
    --from-literal="token=$rtok" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f "$ROOT/gitea/act-runner.yaml"
  kubectl -n "$NS" wait --for=condition=Available deployment/act-runner --timeout=300s
  n="$(curl -sf -H "Authorization: token $atok" http://localhost:30080/api/v1/admin/actions/runners | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
  [ "$n" -ge 1 ] || { echo "runner: not registered"; exit 1; }
  echo "runner: registered ($n)"
}

section_argocd() {
  helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
  helm repo update argo >/dev/null
  helm upgrade --install argocd argo/argo-cd \
    --version 10.15.0 \
    --namespace "$NS" \
    -f "$ROOT/argocd/values.yaml" --wait --timeout 10m
  kubectl -n "$NS" wait --for=condition=Available deployment/argocd-server --timeout=300s
  # Приватный GitHub-репо требует креды для Argo CD; публичный клонируется анонимно.
  if [ "$(curl -s -o /dev/null -w '%{http_code}' 'https://github.com/kryadov/ai-based-isv/info/refs?service=git-upload-pack')" != "200" ]; then
    local gtok
    gtok="${GITHUB_TOKEN:-$(gh auth token 2>/dev/null || true)}"
    [ -n "$gtok" ] || { echo "argocd: репо приватное и нет gh-токена (gh auth login или GITHUB_TOKEN)"; exit 1; }
    kubectl -n "$NS" create secret generic argocd-repo-github-ai-based-isv \
      --from-literal=url=https://github.com/kryadov/ai-based-isv \
      --from-literal=username=kryadov \
      --from-literal="password=$gtok" \
      --type=Opaque \
      --dry-run=client -o yaml \
    | kubectl label --local -f - argocd.argoproj.io/secret-type=repository --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null
    echo "argocd: repo creds wired (private repo)"
  else
    echo "argocd: repo public — anonymous clone"
  fi
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:30081/api/version || true)"
  [ "$code" = "200" ] || { echo "argocd: api not reachable ($code)"; exit 1; }
  echo "argocd: ready (ui http://localhost:30081, repo creds wired)"
}

section_monitoring() {
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update prometheus-community >/dev/null
  local pw
  pw="$(vcli kv get -field=password secret/grafana/admin 2>/dev/null || true)"
  [ -n "$pw" ] || { pw="$(openssl rand -hex 12)"; vcli kv put secret/grafana/admin username=admin "password=$pw" >/dev/null; }
  kubectl -n "$NS" create secret generic grafana-admin \
    --from-literal=username=admin --from-literal="password=$pw" \
    --dry-run=client -o yaml | kubectl apply -f -
  helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
    --version 93.0.0 \
    --namespace "$NS" \
    -f "$ROOT/monitoring/values.yaml" --wait --timeout 15m
  kubectl -n "$NS" wait --for=condition=Available deployment/kube-prometheus-stack-grafana --timeout=300s 2>/dev/null || true
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$pw" http://localhost:30082/api/health || true)"
  [ "$code" = "200" ] || { echo "grafana: not reachable ($code)"; exit 1; }
  echo "monitoring: kube-prometheus-stack ready (grafana http://localhost:30082)"
}

section_loki() {
  helm upgrade --install loki grafana/loki \
    --version 7.5.0 \
    --namespace "$NS" \
    -f "$ROOT/loki/values.yaml" --wait --timeout 10m
  helm upgrade --install alloy grafana/alloy \
    --version 1.15.0 \
    --namespace "$NS" \
    --values "$ROOT/loki/alloy.yaml" --wait --timeout 10m
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:3100/ready || true)"
  # Loki без NodePort снаружи — проверка через порт-форвард kubectl
  if [ "$code" != "200" ]; then
    kubectl -n "$NS" port-forward svc/loki 3100:3100 >/tmp/loki-pf.log 2>&1 &
    PF=$!; sleep 3
    code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:3100/ready || true)"
    kill $PF 2>/dev/null || true
  fi
  [ "$code" = "200" ] || { echo "loki: not ready ($code)"; exit 1; }
  echo "loki: ready; alloy: $(kubectl -n "$NS" get ds alloy -o jsonpath='{.status.numberReady}') ready"
}

section_demo() {
  # Окружения + GitOps: AppProject (граница prod/staging) + Application demo-prod (auto-sync).
  # ВАЖНО: Application смотрит targetRevision=main — запускать после merge инкремента в main.
  kubectl create namespace prod --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl create namespace staging --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl apply -f "$ROOT/../infra/argocd/appproject.yaml"
  kubectl apply -f "$ROOT/../infra/argocd/application-demo.yaml"
  kubectl -n "$NS" wait --for=jsonpath='{.status.sync.status}'=Synced application/demo-prod --timeout=300s
  kubectl -n prod wait --for=condition=Ready pod -l app=podinfo --timeout=300s
  echo "demo: podinfo Running in prod (argocd Synced)"
}

case "${1:-all}" in
  vault)  section_vault ;;
  cnpg)   section_cnpg ;;
  gitea)  section_gitea ;;
  runner) section_runner ;;
  argocd) section_argocd ;;
  monitoring) section_monitoring ;;
  loki)   section_loki ;;
  demo)   section_demo ;;
  all)    section_vault; section_cnpg; section_gitea; section_runner; section_argocd; section_monitoring; section_loki; section_demo ;;
  *) echo "usage: $0 [vault|cnpg|gitea|runner|argocd|monitoring|loki|demo|all]"; exit 2 ;;
esac
