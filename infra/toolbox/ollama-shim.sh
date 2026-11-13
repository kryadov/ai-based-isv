#!/usr/bin/env bash
# `ollama` stand-in inside the toolbox: Ollama runs natively on the Windows
# host, the toolbox only needs `--version` / `list` for `org-bootstrap check`.
# Talks to the host's Ollama HTTP API at $OLLAMA_HOST (set by toolbox.sh).
set -euo pipefail
base="${OLLAMA_HOST:-http://host.docker.internal:11434}"
case "${1:-}" in
  --version|-v)
    v="$(curl -fsS -m 5 "$base/api/version" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')" \
      || { echo "ollama: host API not reachable at $base" >&2; exit 1; }
    echo "ollama version is $v (host, via $base)" ;;
  list|ls)
    curl -fsS -m 10 "$base/api/tags" \
      | python3 -c 'import json,sys; [print(m["name"]) for m in json.load(sys.stdin).get("models", [])]' ;;
  *) echo "ollama shim: only --version and list are supported (host Ollama at $base)" >&2; exit 2 ;;
esac
