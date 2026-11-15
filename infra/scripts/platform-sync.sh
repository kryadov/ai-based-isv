#!/usr/bin/env bash
# Self-development level 2 (spec §13.7): GitHub stays canonical; the lab Gitea
# repo swarm-lab/ai-based-isv is the working copy agents commit to through the
# gate corridor. This script moves commits between the two — never by
# rewriting history and never by pushing GitHub main directly.
#
#   init     create the Gitea org repo (empty), push GitHub main into it, and
#            onboard it (platform check profile, protected main with a push
#            whitelist of swarm-admin only, registry entry). Idempotent.
#   pull     fast-forward Gitea main to GitHub main. Refuses (exit 3) when
#            Gitea main holds commits GitHub lacks — promote them first.
#   promote  push Gitea main to the GitHub branch selfdev/<sha7> and open a
#            GitHub PR (merging it stays an explicit, CI-gated step). After the
#            merge, `pull` fast-forwards Gitea onto the merge commit.
#
# Run from Git Bash on the host (GitHub auth = your git credentials; Gitea at
# localhost:30080). The Gitea token is read from OpenBao secret/gitea/admin
# (or SYNC_GITEA_TOKEN) and travels in GIT_CONFIG_* env, never in URLs/argv.
# Test overrides: SYNC_GITHUB_URL, SYNC_GITEA_URL, SYNC_GITEA_TOKEN,
# SYNC_WORKDIR, SYNC_NO_PR=1 (skip `gh pr create`).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GITHUB_URL="${SYNC_GITHUB_URL:-https://github.com/kryadov/ai-based-isv.git}"
GITEA_API="${SYNC_GITEA_API:-http://localhost:30080/api/v1}"
GITEA_URL="${SYNC_GITEA_URL:-http://localhost:30080/swarm-lab/ai-based-isv.git}"
ORG=swarm-lab
REPO=ai-based-isv

die() { echo "platform-sync: $*" >&2; exit "${2:-1}"; }

gitea_token() {
  if [ -n "${SYNC_GITEA_TOKEN:-}" ]; then echo "$SYNC_GITEA_TOKEN"; return; fi
  local pod
  pod="$(kubectl -n platform get pod -l app.kubernetes.io/name=openbao -o jsonpath='{.items[0].metadata.name}')"
  kubectl -n platform exec -i "$pod" -- sh -c \
    'BAO_TOKEN=$(head -c 128); export BAO_TOKEN; BAO_ADDR=http://127.0.0.1:8200 bao kv get -field=token secret/gitea/admin' \
    < "$ROOT/infra/.vault-root-token"
}

# git against Gitea with the token as an extra header (env, not argv).
gitea_git() {
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="http.$GITEA_URL.extraHeader" \
    GIT_CONFIG_VALUE_0="Authorization: token $TOKEN" git -C "$WORK" "$@"
}

setup() {
  TOKEN="$(gitea_token)"
  [ -n "$TOKEN" ] || die "no Gitea token (OpenBao sealed? run bootstrap.sh vault)"
  if [ -n "${SYNC_WORKDIR:-}" ]; then WORK="$SYNC_WORKDIR"; else WORK="$(mktemp -d)"; fi
  [ -d "$WORK/objects" ] || git init -q --bare "$WORK"
  git -C "$WORK" fetch -q "$GITHUB_URL" "+refs/heads/main:refs/remotes/github/main"
}

fetch_gitea() {
  gitea_git fetch -q "$GITEA_URL" "+refs/heads/main:refs/remotes/gitea/main"
}

cmd_pull() {
  setup; fetch_gitea
  local gh gt
  gh="$(git -C "$WORK" rev-parse refs/remotes/github/main)"
  gt="$(git -C "$WORK" rev-parse refs/remotes/gitea/main)"
  if [ "$gh" = "$gt" ]; then echo "platform-sync: Gitea main already at ${gh:0:7}"; return; fi
  git -C "$WORK" merge-base --is-ancestor "$gt" "$gh" \
    || die "Gitea main ${gt:0:7} has unpromoted commits not on GitHub — run: platform-sync.sh promote" 3
  gitea_git push -q "$GITEA_URL" "refs/remotes/github/main:refs/heads/main"
  echo "platform-sync: Gitea main ${gt:0:7} → ${gh:0:7}"
}

cmd_promote() {
  setup; fetch_gitea
  local gt short branch
  gt="$(git -C "$WORK" rev-parse refs/remotes/gitea/main)"
  if git -C "$WORK" merge-base --is-ancestor "$gt" refs/remotes/github/main; then
    echo "platform-sync: nothing to promote (Gitea main ${gt:0:7} is on GitHub)"; return
  fi
  short="${gt:0:7}"; branch="selfdev/$short"
  git -C "$WORK" push -q "$GITHUB_URL" "refs/remotes/gitea/main:refs/heads/$branch"
  echo "platform-sync: pushed $branch"
  [ -n "${SYNC_NO_PR:-}" ] && return
  local body
  body="$(printf 'Self-development promotion (spec §13.7 level 2): Gitea `%s/%s` main %s.\n\nMerged in the Gitea corridor (CI + bot review + HITL):\n%s\n\nMerge after the GitHub CI is green; then run `platform-sync.sh pull`.\n' \
    "$ORG" "$REPO" "$short" \
    "$(git -C "$WORK" log --merges --format='- %s' refs/remotes/github/main..refs/remotes/gitea/main)")"
  gh pr create --repo kryadov/ai-based-isv --base main --head "$branch" \
    --title "selfdev: promote Gitea main $short" --body "$body"
}

cmd_init() {
  TOKEN="$(gitea_token)"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: token $TOKEN" "$GITEA_API/repos/$ORG/$REPO")"
  if [ "$code" != "200" ]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: token $TOKEN" \
      -H 'Content-Type: application/json' -X POST "$GITEA_API/orgs/$ORG/repos" \
      -d "{\"name\":\"$REPO\",\"private\":true,\"auto_init\":false,\"default_branch\":\"main\"}")"
    [ "$code" = "201" ] || die "could not create $ORG/$REPO ($code) — run bootstrap.sh agent-user first (creates org $ORG)"
    echo "platform-sync: created $ORG/$REPO"
  fi
  # Seed main from GitHub while main is still unprotected (or fast-forward it).
  setup
  if gitea_git fetch -q "$GITEA_URL" "+refs/heads/main:refs/remotes/gitea/main" 2>/dev/null; then
    cmd_pull
  else
    gitea_git push -q "$GITEA_URL" "refs/remotes/github/main:refs/heads/main"
    echo "platform-sync: seeded $ORG/$REPO main from GitHub"
  fi
  "$ROOT/infra/scripts/toolbox.sh" uv run org-bootstrap onboard --project "$REPO" --owner "$ORG" \
    --kind tool --stack platform --reason "self-development level 2 (spec §13.7)" \
    --push-whitelist swarm-admin
}

case "${1:-}" in
  init) cmd_init ;;
  pull) cmd_pull ;;
  promote) cmd_promote ;;
  *) echo "usage: $0 init|pull|promote" >&2; exit 2 ;;
esac
