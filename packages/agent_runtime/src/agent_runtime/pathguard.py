"""``paths_allow`` guard — post-check of the agent's diff against the vertex allow list.

The executor returns a unified diff; we derive which files it touched and verify
every touched path is allowed by the vertex's ``paths_allow`` glob list. An empty
allow list allows nothing: a vertex with no declared scope must not change any file.

Fail-closed policy:

* deletions are changed files too — ``--- a/<path>`` headers (their ``+++`` side is
  ``/dev/null``) enter the touched set, so an agent cannot bypass the guard by
  removing a protected file; renames and copies contribute both sides;
* a *non-empty* diff from which no path can be derived raises :class:`DiffUnparseable`
  instead of reading as "no changes" — an unexpected diff shape must never silently
  disable path enforcement. Only a genuinely empty diff means "nothing changed".

The Task 9 worker treats ``DiffUnparseable`` as a vertex FAIL (its blanket
"any exception on a vertex → fail" already covers it). Duplicate headers (the
``---``/``+++`` pair of a modified file) collapse into one path, diff order kept.
"""

from __future__ import annotations

import fnmatch
import re

_ADDED_MARKER = "+++ "
_REMOVED_MARKER = "--- "
_GIT_HEADER_MARKER = "diff --git "
_GIT_HEADER_RE = re.compile(r"diff --git a/(\S+) b/(\S+)")
_GIT_SECTION_RE = re.compile(r"(?m)^(?=diff --git )")
_METADATA_MARKERS = (
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "old mode ",
    "new mode ",
)
_DEV_NULL = "/dev/null"
_EXCERPT_CHARS = 120


class DiffUnparseable(ValueError):
    """The diff is non-empty but its changed paths cannot be derived safely."""


def _header_path(rest: str, *, side_prefix: str) -> str | None:
    """Clean one ``+++``/``---`` header payload: strip the tab-separated timestamp,
    the ``a/`` or ``b/`` side prefix, and reject ``/dev/null`` or empty targets."""
    target = rest.split("\t", 1)[0].strip()
    if target.startswith(side_prefix):
        target = target[len(side_prefix) :]
    if not target or target == _DEV_NULL:
        return None
    return target


def _git_header_paths(line: str) -> tuple[str, str] | None:
    """Extract both sides of an unquoted ``diff --git a/<old> b/<new>`` header."""
    match = _GIT_HEADER_RE.fullmatch(line)
    if match is None:
        return None
    return match.group(1), match.group(2)


def diff_files(diff: str) -> list[str]:
    """Touched paths in diff order, deduplicated, with per-section fail-closed parsing.

    ``+++ b/<path>`` and ``--- a/<path>`` remain the primary source. A recognized
    rename, copy, or mode-only section also contributes both paths from its
    ``diff --git`` header. Every Git section must contribute a path independently,
    so surplus paths from one section cannot hide an unrecognized binary section.
    """
    if not diff.strip():
        return []
    paths: list[str] = []
    seen: set[str] = set()
    head = diff.strip().splitlines()[0][:_EXCERPT_CHARS]

    for section in _GIT_SECTION_RE.split(diff):
        lines = section.splitlines()
        is_git_section = bool(lines and lines[0].startswith(_GIT_HEADER_MARKER))
        git_paths = _git_header_paths(lines[0]) if is_git_section else None
        if is_git_section and git_paths is None:
            raise DiffUnparseable(f"unrecognized git header, first line: {head!r}")

        section_has_content = False
        section_derives_path = False
        for line in lines:
            if line.startswith(_ADDED_MARKER):
                path = _header_path(line.removeprefix(_ADDED_MARKER), side_prefix="b/")
                section_has_content = True
            elif line.startswith(_REMOVED_MARKER):
                path = _header_path(line.removeprefix(_REMOVED_MARKER), side_prefix="a/")
                section_has_content = True
            else:
                continue
            if path is not None:
                section_derives_path = True
                if path not in seen:
                    seen.add(path)
                    paths.append(path)

        has_recognized_metadata = any(line.startswith(_METADATA_MARKERS) for line in lines[1:])
        if git_paths is not None and (section_has_content or has_recognized_metadata):
            section_derives_path = True
            for path in git_paths:
                if path not in seen:
                    seen.add(path)
                    paths.append(path)

        if is_git_section and not section_derives_path:
            raise DiffUnparseable(
                f"git section contributes no derivable path, first line: {head!r}"
            )

    if not paths:
        raise DiffUnparseable(
            "diff contains no usable path headers, refusing to read it as 'no changes'; "
            f"first line: {head!r}"
        )
    return paths


def filter_violations(changed: list[str], paths_allow: list[str]) -> list[str]:
    """Changed paths not matched by any allow-list pattern (empty list allows nothing)."""
    return [
        path
        for path in changed
        if not any(fnmatch.fnmatch(path, pattern) for pattern in paths_allow)
    ]
