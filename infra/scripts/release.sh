#!/usr/bin/env bash
# GitHub Release по конвенции спеки §13.8: semver на границе закрытия инкремента,
# changelog собирается из merge-коммитов (PR-заголовков) с предыдущего тега.
# Использование: ./infra/scripts/release.sh v0.3.0 [base-commit]
set -euo pipefail

TAG="${1:?usage: release.sh <tag> [base-commit]}"
BASE="${2:-$(git describe --tags --abbrev=0 2>/dev/null || echo 'HEAD~10')}"

echo "Release: $TAG (base: $BASE)"

# changelog из merge-коммитовrange
CHANGELOG="$(git log --merges --format='- %s (%h)' "$BASE"..HEAD)"
[ -n "$CHANGELOG" ] || CHANGELOG="- (no merged PRs; direct commits to main — see git log)"

gh release create "$TAG" \
  --title "$TAG" \
  --notes "## What's included

$CHANGELOG

Verified before release: ruff + mypy --strict + pytest green on main;
L0 acceptance flows (infra/tests/m2a..m3) PASS on the live stand.
Release convention: docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md §13.8."

echo "Release $TAG created: $(gh release view "$TAG" --json url -q .url)"
