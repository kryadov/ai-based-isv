#!/usr/bin/env bash
# Cluster-flavour helpers shared by the bootstrapper and the acceptance flows.
# SWARM_CLUSTER selects the stand cluster:
#   k3d              (default) — k3d cluster `swarm-lab`; host ports and images
#                    must be pushed into it explicitly (port-add / image import).
#   rancher-desktop  — Rancher Desktop's built-in k3s (moby engine) on Windows:
#                    k3s uses the host docker image store and Rancher forwards
#                    every NodePort to localhost, so both steps are no-ops.
# Source it: . "$(dirname "$0")/cluster-lib.sh"

swarm_cluster() { printf '%s' "${SWARM_CLUSTER:-k3d}"; }

# Make a locally built docker image available to the cluster's kubelet.
cluster_image_import() {
  local img="$1"
  case "$(swarm_cluster)" in
    rancher-desktop) docker image inspect "$img" >/dev/null ;;
    *) k3d image import "$img" -c swarm-lab >/dev/null ;;
  esac
}

# True when NodePort $1 is reachable from the host.
cluster_nodeport_published() {
  local port="$1"
  case "$(swarm_cluster)" in
    rancher-desktop) return 0 ;;
    *) docker port k3d-swarm-lab-serverlb 2>/dev/null | grep -q "^$port/tcp" ;;
  esac
}

# Publish NodePort (or range "a-b") $1 on the host once; idempotent.
cluster_publish_nodeport() {
  local spec="$1" first="${1%%-*}"
  if cluster_nodeport_published "$first"; then
    return 0
  fi
  command -v k3d >/dev/null 2>&1 || { echo "cluster: k3d not found — cannot publish NodePort $spec" >&2; return 1; }
  k3d cluster edit swarm-lab --port-add "$spec:$spec@loadbalancer" \
    && echo "cluster: published k3d NodePort $spec"
}

# Bring the stand cluster up (or verify it) and select its kube context.
cluster_up() {
  case "$(swarm_cluster)" in
    rancher-desktop) bash "$(dirname "${BASH_SOURCE[0]}")/rancher-up.sh" ;;
    *) bash "$(dirname "${BASH_SOURCE[0]}")/k3d-up.sh" ;;
  esac
}
