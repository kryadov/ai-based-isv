"""``paths_allow`` guard — post-check of the agent's diff against the vertex allow list.

The executor returns a unified diff; we derive which files it touched and verify
every touched path is allowed by the vertex's ``paths_allow`` glob list. An empty
allow list allows nothing: a vertex with no declared scope must not change any file.
Fail-closed: the caller fails the vertex when this returns any violations.
"""

from __future__ import annotations

import fnmatch

_HEADER_PREFIX = "+++ "
_DEV_NULL = "/dev/null"


def diff_files(diff: str) -> list[str]:
    """Touched paths taken from ``+++ b/<path>`` header lines, in diff order.

    ``/dev/null`` headers (deletions) are skipped; a tab-separated timestamp
    suffix on the header (emitted by some diff tools) is stripped.
    """
    paths: list[str] = []
    for line in diff.splitlines():
        if not line.startswith(_HEADER_PREFIX):
            continue
        target = line.removeprefix(_HEADER_PREFIX).split("\t", 1)[0].strip()
        if target.startswith("b/"):
            target = target.removeprefix("b/")
        if not target or target == _DEV_NULL:
            continue
        paths.append(target)
    return paths


def filter_violations(changed: list[str], paths_allow: list[str]) -> list[str]:
    """Changed paths not matched by any allow-list pattern (empty list allows nothing)."""
    return [
        path
        for path in changed
        if not any(fnmatch.fnmatch(path, pattern) for pattern in paths_allow)
    ]
