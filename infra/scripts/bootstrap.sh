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

section_gitea() {
  helm repo add gitea https://dl.gitea.com/charts/ >/dev/null 2>&1 || true
  helm repo update gitea >/dev/null
  local pw
  pw="$(openssl rand -hex 16)"
  helm upgrade --install gitea gitea/gitea \
    --namespace "$NS" \
    --set gitea.admin.username=swarm-admin \
    --set "gitea.admin.password=$pw" \
    --values "$ROOT/gitea/values.yaml" --wait --timeout 10m
  kubectl -n "$NS" wait --for=condition=Ready pod -l app.kubernetes.io/name=gitea --timeout=300s

  # токен администратора: если в Vault валидный — не пересоздаём
  local old tok code
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

case "${1:-all}" in
  vault) section_vault ;;
  gitea) section_gitea ;;
  all)   section_vault; section_gitea ;;
  *) echo "usage: $0 [vault|gitea|all]"; exit 2 ;;
esac
