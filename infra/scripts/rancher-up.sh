#!/usr/bin/env bash
# Verify the Rancher Desktop k3s stand cluster (SWARM_CLUSTER=rancher-desktop).
# Rancher Desktop owns the cluster lifecycle; this script only checks the
# invariants the stand relies on and fails with a fix hint otherwise.
set -euo pipefail

ctx="$(kubectl config current-context 2>/dev/null || true)"
if [ "$ctx" != "rancher-desktop" ]; then
  kubectl config use-context rancher-desktop >/dev/null 2>&1 \
    || { echo "rancher: kube context rancher-desktop not found — enable Kubernetes in Rancher Desktop" >&2; exit 1; }
fi
kubectl wait --for=condition=Ready nodes --all --timeout=120s >/dev/null
runtime="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}')"
case "$runtime" in
  docker://*) ;;
  *) echo "rancher: container runtime is '$runtime' — select the moby (dockerd) engine so k3s sees docker-built images" >&2; exit 1 ;;
esac
# The stand publishes Postgres on NodePort 15432 (infra/rancher/swarm-lab.start widens the range).
# "already allocated" means the stand's own Postgres NodePort holds it (a
# re-apply) — the range is fine; only an out-of-range rejection is fatal.
if ! probe_err="$(kubectl create service nodeport swarm-range-probe --tcp=5432 --node-port=15432 --dry-run=server -o name 2>&1 >/dev/null)"; then
  case "$probe_err" in
    *"already allocated"*) ;;
    *) echo "rancher: NodePort 15432 rejected ($probe_err) — install infra/rancher/swarm-lab.start (infra/scripts/toolbox.sh does it) and restart Rancher Desktop" >&2; exit 1 ;;
  esac
fi
kubectl get nodes -o wide
echo "rancher: k3s Ready (runtime $runtime, NodePort range ok)"
