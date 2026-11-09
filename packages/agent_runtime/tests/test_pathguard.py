"""Tests for the paths_allow guard over the executor's diff.

Post-check of the M4 design: every file the agent touched must be inside the
vertex's allow list; an empty allow list means the agent may change nothing.
"""

from __future__ import annotations

from agent_runtime.pathguard import diff_files, filter_violations

_MODIFY = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1234567..89abcde 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)
_ADD = (
    "diff --git a/README.md b/README.md\n"
    "new file mode 100644\n"
    "index 0000000..1234567\n"
    "--- /dev/null\n"
    "+++ b/README.md\n"
    "@@ -0,0 +1 @@\n"
    "+# hello\n"
)
_DELETE = (
    "diff --git a/old.txt b/old.txt\n"
    "deleted file mode 100644\n"
    "--- a/old.txt\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-gone\n"
)


def test_diff_files_parses_paths() -> None:
    assert diff_files(_MODIFY + _ADD + _DELETE) == ["src/app.py", "README.md"]
    assert diff_files("") == []


def test_diff_files_strips_timestamp() -> None:
    # some diff tools append a tab-separated timestamp to the header line
    assert diff_files("--- a/x.py\n+++ b/x.py\t2026-11-09 12:00:00 +0000\n") == ["x.py"]


def test_allow_violation_detected() -> None:
    changed = ["src/app.py", "docs/notes.md"]
    assert filter_violations(changed, ["src/**", "pyproject.toml"]) == ["docs/notes.md"]


def test_empty_allow_list_violates_everything() -> None:
    changed = ["src/app.py", "docs/notes.md"]
    assert filter_violations(changed, []) == changed


def test_glob_allows_subdir() -> None:
    assert filter_violations(["tests/unit/test_x.py"], ["tests/**"]) == []
    assert filter_violations(["src/wordtools/reverse.py"], ["src/wordtools/**"]) == []
