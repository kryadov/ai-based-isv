#!/usr/bin/env bash
# Top-level idempotent L0 bootstrap: stand cluster (k3d / Rancher Desktop) → all sections → env manifest.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=scripts/cluster-lib.sh
. "$ROOT/scripts/cluster-lib.sh"
cluster_up
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
