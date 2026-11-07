#!/usr/bin/env bash
# Верхнеуровневый идемпотентный бутстреп L0-ядра: k3d → Vault → CNPG → Gitea → runner → env-манифест.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

"$ROOT/scripts/k3d-up.sh"
"$ROOT/scripts/bootstrap.sh" vault
"$ROOT/scripts/bootstrap.sh" cnpg
"$ROOT/scripts/bootstrap.sh" gitea
"$ROOT/scripts/bootstrap.sh" runner
"$ROOT/scripts/gen-env.sh"

echo "bootstrap: L0 core ready"
