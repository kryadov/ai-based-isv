#!/usr/bin/env bash
# Верхнеуровневый идемпотентный бутстреп L0: k3d → все секции → env-манифест.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

"$ROOT/scripts/k3d-up.sh"
"$ROOT/scripts/bootstrap.sh" vault
"$ROOT/scripts/bootstrap.sh" cnpg
"$ROOT/scripts/bootstrap.sh" gitea
"$ROOT/scripts/bootstrap.sh" runner
"$ROOT/scripts/bootstrap.sh" argocd
"$ROOT/scripts/bootstrap.sh" monitoring
"$ROOT/scripts/bootstrap.sh" loki
"$ROOT/scripts/bootstrap.sh" demo
"$ROOT/scripts/bootstrap.sh" openproject
"$ROOT/scripts/gen-env.sh"

echo "bootstrap: L0 ready"
