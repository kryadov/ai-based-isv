#!/usr/bin/env bash
# Секции L0; верхнеуровневый вход появится в Task 7 (infra/bootstrap.sh).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NS=platform

section_vault() {
  helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null 2>&1 || true
  helm repo update hashicorp >/dev/null
  helm upgrade --install vault hashicorp/vault \
    --namespace "$NS" --create-namespace \
    -f "$ROOT/vault/values.yaml" --wait --timeout 5m
  kubectl -n "$NS" wait --for=condition=Ready pod -l app.kubernetes.io/name=vault --timeout=180s
  kubectl -n "$NS" exec vault-0 -- vault status -address=http://127.0.0.1:8200 | grep -E "Sealed|Initialized"
}

# CLI-хелперы Vault через exec в pod
vcli() { kubectl -n "$NS" exec vault-0 -- env VAULT_ADDR=http://127.0.0.1:8200 vault "$@"; }

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
  local pw
  pw="$(vcli kv get -field=password secret/gitea/admin 2>/dev/null || true)"
  [ -n "$pw" ] || { pw="$(openssl rand -hex 16)"; vcli kv put secret/gitea/admin username=swarm-admin "password=$pw" >/dev/null; }
  helm upgrade --install gitea gitea/gitea \
    --namespace "$NS" \
    --set gitea.admin.username=swarm-admin \
    --set "gitea.admin.password=$pw" \
    --values "$ROOT/gitea/values.yaml" --wait --timeout 10m
  kubectl -n "$NS" wait --for=condition=Ready pod -l app.kubernetes.io/name=gitea --timeout=300s 2>/dev/null || true

  ensure_admin_token
}

section_gitea_db() {
  # Переключение Gitea на внешний CNPG (пароли admin/DB — из Vault и secret postgres-platform-app).
  local dbpw pw
  dbpw="$(kubectl -n "$NS" get secret postgres-platform-app -o jsonpath='{.data.password}' | base64 -d)"
  pw="$(vcli kv get -field=password secret/gitea/admin 2>/dev/null || true)"
  [ -n "$pw" ] || { pw="$(openssl rand -hex 16)"; vcli kv put secret/gitea/admin username=swarm-admin "password=$pw" >/dev/null; }
  helm upgrade --install gitea gitea/gitea \
    --namespace "$NS" \
    --set gitea.admin.username=swarm-admin \
    --set "gitea.admin.password=$pw" \
    --set "gitea.config.database.PASSWD=$dbpw" \
    --values "$ROOT/gitea/values.yaml" \
    --values "$ROOT/gitea/values-external-db.yaml" --wait --timeout 10m
  kubectl -n "$NS" wait --for=condition=Ready pod -l app.kubernetes.io/name=gitea --timeout=300s 2>/dev/null || true
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:30080/api/v1/version || true)"
  [ "$code" = "200" ] || { echo "gitea: external-db switch failed (version api $code)"; exit 1; }
  echo "gitea: external db ok"
}

section_runner() {
  # Регистрационный токен runner'а из Gitea admin API → secret → apply.
  local rtok code n
  ensure_admin_token
  local atok; atok="$(vcli kv get -field=token secret/gitea/admin)"
  rtok="$(curl -sf -X POST -H "Authorization: token $atok" http://localhost:30080/api/v1/admin/actions/runners/registration-token | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')"
  kubectl -n "$NS" create secret generic act-runner-registration \
    --from-literal="token=$rtok" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f "$ROOT/gitea/act-runner.yaml"
  kubectl -n "$NS" wait --for=condition=Available deployment/act-runner --timeout=300s
  local n
  n="$(curl -sf -H "Authorization: token $atok" http://localhost:30080/api/v1/admin/actions/runners | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
  [ "$n" -ge 1 ] || { echo "runner: not registered"; exit 1; }
  echo "runner: registered ($n)"
}

case "${1:-all}" in
  vault)      section_vault ;;
  gitea)      section_gitea ;;
  gitea_db)   section_gitea_db ;;
  runner)     section_runner ;;
  all)        section_vault; section_gitea; section_gitea_db; section_runner ;;
  *) echo "usage: $0 [vault|gitea|gitea_db|runner|all]"; exit 2 ;;
esac
