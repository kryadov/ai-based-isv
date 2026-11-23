#!/usr/bin/env bash
# Run the platform as a standing service on the stand (M11). From Git Bash at
# the repo root:
#
#   bash infra/scripts/platform.sh up [--ref <git ref>]   # default: origin/main
#   bash infra/scripts/platform.sh status | logs [N] | restart | down | password
#
# `up` (idempotent):
#   1. pins the code: git worktree .platform-live at the ref (editing the dev
#      tree never changes the running platform; `up` again moves the pin);
#   2. builds the control-plane image swarm-platform:<sha7> and deploys it as
#      the Argo CD app platform-control (bootstrap.sh platform) — the panel and
#      the API on http://localhost:30400;
#   3. (re)starts the daemon container swarm-platform-daemon (toolbox image,
#      --restart unless-stopped, own venv volume): the dispatcher picks up
#      issues labelled `swarm` in onboarded repos; the automation poller runs
#      panel requests and cron schedules.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
TB="$ROOT/infra/scripts/toolbox.sh"
LIVE=.platform-live
NAME=swarm-platform-daemon
RD_BIN="/c/Program Files/Rancher Desktop/resources/resources/win32/bin"
command -v docker >/dev/null 2>&1 || export PATH="$RD_BIN:$PATH"

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

repo_daemon_path() {
  local p
  p="$(pwd -W 2>/dev/null || pwd)"
  case "$p" in
    [A-Za-z]:/*) printf '/mnt/%s%s' "$(printf '%s' "${p:0:1}" | tr 'A-Z' 'a-z')" "${p:2}" ;;
    *) printf '%s' "$p" ;;
  esac
}

stop_daemon() {
  # SIGTERM first (M12): workers release their vertices and tear sandboxes down
  # within the container stop timeout; only then is the container removed.
  docker inspect "$NAME" >/dev/null 2>&1 || return 1
  docker stop "$NAME" >/dev/null 2>&1 || true
  docker rm "$NAME" >/dev/null 2>&1 || true
}

start_daemon() {
  local repo
  repo="$(repo_daemon_path)"
  stop_daemon || true
  TOOLBOX_DETACH="$NAME" TOOLBOX_VENV=swarm-platform-venv TOOLBOX_WORKDIR="$LIVE" \
    "$TB" env \
      SWARM_DAEMON_REPO_ROOT="$repo" \
      SWARM_DAEMON_WORKSPACE_DIR="$repo/.agent-runtime-workspace/daemon" \
      PYTHONUNBUFFERED=1 \
      bash -c 'uv sync -q --all-packages --frozen && exec /opt/venv/bin/python -m agent_runtime.daemon' \
      >/dev/null
  echo "platform: daemon $NAME started (logs: platform.sh logs)"
}

cmd_up() {
  local ref="origin/main" sha
  if [ "${1:-}" = "--ref" ]; then ref="${2:?--ref needs a value}"; fi
  git fetch -q origin
  sha="$(git rev-parse --verify "$ref^{commit}")"
  if [ -d "$LIVE" ]; then
    git -C "$LIVE" checkout -q --detach "$sha"
  else
    git worktree add -q --detach "$LIVE" "$sha"
  fi
  echo "platform: pinned $LIVE at ${sha:0:7} ($ref)"
  "$TB" bash infra/scripts/bootstrap.sh vault
  "$TB" docker build -q -t "swarm-platform:${sha:0:7}" -f "$LIVE/infra/platform/Dockerfile" "$LIVE" >/dev/null
  echo "platform: image swarm-platform:${sha:0:7} built"
  "$TB" bash infra/scripts/bootstrap.sh platform "$sha"
  start_daemon
}

cmd_status() {
  local live
  live="$(git -C "$LIVE" rev-parse --short HEAD 2>/dev/null || echo none)"
  echo "pinned:  $live"
  echo "daemon:  $(docker inspect -f '{{.State.Status}} (restarts: {{.RestartCount}}, since {{.State.StartedAt}})' "$NAME" 2>/dev/null || echo 'not created')"
  echo "panel:   http://localhost:30400/ui ($(curl -s -m 3 -o /dev/null -w '%{http_code}' http://localhost:30400/healthz || echo down))"
  "$TB" kubectl -n platform get application platform-control \
    -o jsonpath='argo:    {.status.sync.status}/{.status.health.status} @ {.status.sync.revision}{"\n"}' 2>/dev/null \
    || echo "argo:    platform-control not deployed"
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  status) cmd_status ;;
  logs) docker logs --tail "${2:-100}" "$NAME" ;;
  restart) docker restart "$NAME" >/dev/null && echo "platform: daemon restarted" ;;
  down) if stop_daemon; then echo "platform: daemon stopped (control plane stays up)"; else echo "platform: daemon not running"; fi ;;
  password) "$TB" bash infra/scripts/bootstrap.sh platform-password ;;
  *) usage ;;
esac
