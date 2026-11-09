"""``paths_allow`` guard — post-check of the agent's diff against the vertex allow list.

The executor returns a unified diff; we derive which files it touched and verify
every touched path is allowed by the vertex's ``paths_allow`` glob list. An empty
allow list allows nothing: a vertex with no declared scope must not change any file.

Fail-closed policy:

* deletions are changed files too — ``--- a/<path>`` headers (their ``+++`` side is
  ``/dev/null``) enter the touched set, so an agent cannot bypass the guard by
  removing a protected file; renames contribute both sides;
* a *non-empty* diff from which no path can be derived raises :class:`DiffUnparseable`
  instead of reading as "no changes" — an unexpected diff shape must never silently
  disable path enforcement. Only a genuinely empty diff means "nothing changed".

The Task 9 worker treats ``DiffUnparseable`` as a vertex FAIL (its blanket
"any exception on a vertex → fail" already covers it). Duplicate headers (the
``---``/``+++`` pair of a modified file) collapse into one path, diff order kept.
"""

from __future__ import annotations

import fnmatch

_ADDED_MARKER = "+++ "
_REMOVED_MARKER = "--- "
_DEV_NULL = "/dev/null"
_EXCERPT_CHARS = 120


class DiffUnparseable(ValueError):
    """The diff text is non-empty but no changed file path could be derived from it."""


def _header_path(rest: str, *, side_prefix: str) -> str | None:
    """Clean one ``+++``/``---`` header payload: strip the tab-separated timestamp,
    the ``a/`` or ``b/`` side prefix, and reject ``/dev/null`` or empty targets."""
    target = rest.split("\t", 1)[0].strip()
    if target.startswith(side_prefix):
        target = target[len(side_prefix) :]
    if not target or target == _DEV_NULL:
        return None
    return target


def diff_files(diff: str) -> list[str]:
    """Touched paths (added, modified and deleted files) in diff order, deduplicated.

    Paths come from ``+++ b/<path>`` headers and — for deletions and renames —
    ``--- a/<path>`` headers; ``/dev/null`` sides are skipped.
    """
    if not diff.strip():
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(_ADDED_MARKER):
            path = _header_path(line.removeprefix(_ADDED_MARKER), side_prefix="b/")
        elif line.startswith(_REMOVED_MARKER):
            path = _header_path(line.removeprefix(_REMOVED_MARKER), side_prefix="a/")
        else:
            continue
        if path is None or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    if not paths:
        head = diff.strip().splitlines()[0][:_EXCERPT_CHARS]
        raise DiffUnparseable(
            f"diff is not empty but contains no file path headers, refusing to read it "
            f"as 'no changes'; first line: {head!r}"
        )
    return paths


def filter_violations(changed: list[str], paths_allow: list[str]) -> list[str]:
    """Changed paths not matched by any allow-list pattern (empty list allows nothing)."""
    return [
        path
        for path in changed
        if not any(fnmatch.fnmatch(path, pattern) for pattern in paths_allow)
    ]
