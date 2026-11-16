#!/usr/bin/env bash
# Run a command inside the swarm-lab toolbox container (Windows + Rancher
# Desktop stand). Usage, from Git Bash at the repo root:
#
#   ./infra/scripts/toolbox.sh                       # interactive shell
#   ./infra/scripts/toolbox.sh uv run pytest         # any command
#   ./infra/scripts/toolbox.sh bash infra/tests/m4c_flow.sh
#
# The cluster is Rancher Desktop's built-in k3s (moby engine): images built
# with `docker build` are visible to k3s directly and NodePorts are forwarded
# to localhost, so the bootstrapper runs with SWARM_CLUSTER=rancher-desktop
# (no k3d import / port-add). INSTALL.md "Windows (Rancher Desktop)".
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RD_BIN="/c/Program Files/Rancher Desktop/resources/resources/win32/bin"
command -v docker >/dev/null 2>&1 || export PATH="$RD_BIN:$PATH"
command -v docker >/dev/null 2>&1 || { echo "toolbox: docker CLI not found (Rancher Desktop installed?)" >&2; exit 1; }

# Windows path C:/Users/... -> the daemon-side path /mnt/c/Users/...
to_daemon_path() {
  local p
  p="$(cd "$1" && pwd -W 2>/dev/null || pwd)"
  case "$p" in
    [A-Za-z]:/*) printf '/mnt/%s%s' "$(printf '%s' "${p:0:1}" | tr 'A-Z' 'a-z')" "${p:2}" ;;
    *) printf '%s' "$p" ;;
  esac
}
REPO="$(to_daemon_path "$ROOT")"

# Rancher Desktop provisioning (k3s NodePort range + inotify limits) — runs
# before k3s on every Rancher start; a changed script needs a Rancher restart.
if [ -n "${LOCALAPPDATA:-}" ]; then
  prov_dir="$(cygpath -u "$LOCALAPPDATA" 2>/dev/null || printf '%s' "$LOCALAPPDATA")/rancher-desktop/provisioning"
  if [ -d "$prov_dir" ] && ! cmp -s "$ROOT/infra/rancher/swarm-lab.start" "$prov_dir/swarm-lab.start"; then
    cp "$ROOT/infra/rancher/swarm-lab.start" "$prov_dir/swarm-lab.start"
    echo "toolbox: installed $prov_dir/swarm-lab.start — restart Rancher Desktop to apply it" >&2
  fi
fi

# Image tag follows the toolbox sources, so edits rebuild it automatically.
tag="$(sha256sum < "$ROOT/infra/toolbox/Dockerfile" | cut -c1-12)"
img="swarm-toolbox:$tag"
if ! docker image inspect "$img" >/dev/null 2>&1; then
  echo "toolbox: building $img" >&2
  docker build -q -t "$img" "$ROOT/infra/toolbox" >/dev/null
fi

home_win="${USERPROFILE:-$HOME}"
KUBECONFIG_DAEMON="$(to_daemon_path "$home_win/.kube")/config"

tty_flags=(-i)
[ -t 0 ] && [ -t 1 ] && tty_flags=(-it)
[ "$#" -gt 0 ] || set -- bash

# Detached mode (M11, platform.sh): TOOLBOX_DETACH=<container name> runs the
# command as a long-lived container restarted with Rancher Desktop; it gets its
# own venv volume (TOOLBOX_VENV) so dev runs never re-sync the running
# platform's environment, and TOOLBOX_WORKDIR (relative to the repo) as cwd.
run_flags=(--rm "${tty_flags[@]}")
[ -n "${TOOLBOX_DETACH:-}" ] && run_flags=(-d --restart unless-stopped --name "$TOOLBOX_DETACH")
venv="${TOOLBOX_VENV:-swarm-toolbox-venv}"
workdir="$REPO${TOOLBOX_WORKDIR:+/$TOOLBOX_WORKDIR}"

# MSYS must not rewrite the Linux paths below into Windows paths.
MSYS_NO_PATHCONV=1 exec docker run "${run_flags[@]}" \
  --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$REPO:$REPO" -w "$workdir" \
  -v "$KUBECONFIG_DAEMON:/root/.kube/config:ro" \
  -v "$venv:/opt/venv" \
  -v swarm-toolbox-uv-cache:/root/.cache/uv \
  -v swarm-toolbox-uv-python:/opt/uv-python \
  -e UV_PROJECT_ENVIRONMENT=/opt/venv \
  -e SWARM_CLUSTER=rancher-desktop \
  -e GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
  "$img" "$@"
