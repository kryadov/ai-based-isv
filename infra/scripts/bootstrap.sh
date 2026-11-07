#!/usr/bin/env bash
# Секции L0 вызываются верхнеуровневым infra/bootstrap.sh (Task 7); сейчас — точка входа для отдельных прогонов.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

section_vault() {
  helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null 2>&1 || true
  helm repo update hashicorp >/dev/null
  helm upgrade --install vault hashicorp/vault \
    --namespace platform --create-namespace \
    -f "$ROOT/vault/values.yaml" --wait --timeout 5m
  kubectl -n platform wait --for=condition=Ready pod -l app.kubernetes.io/name=vault --timeout=180s
  kubectl -n platform exec vault-0 -- vault status -address=http://127.0.0.1:8200 | grep -E "Sealed|Initialized"
}

"${1:-section_vault}"
