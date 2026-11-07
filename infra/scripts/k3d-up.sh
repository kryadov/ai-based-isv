#!/usr/bin/env bash
# Идемпотентный подъём k3d-кластера swarm-lab (конфиг: infra/k3d/config.yaml).
set -euo pipefail

if k3d cluster get swarm-lab >/dev/null 2>&1; then
  echo "k3d: cluster swarm-lab exists"
else
  k3d cluster create swarm-lab --config "$(dirname "$0")/../k3d/config.yaml"
fi

kubectl config use-context k3d-swarm-lab >/dev/null
kubectl get nodes -o wide
kubectl wait --for=condition=Ready nodes --all --timeout=120s
echo "k3d: nodes Ready"
