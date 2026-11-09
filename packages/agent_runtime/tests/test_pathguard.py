"""Tests for the paths_allow guard over the executor's diff.

Post-check of the M4 design: every file the agent touched must be inside the
vertex's allow list; an empty allow list means the agent may change nothing.
Fail-closed: deletions are changed files too, and a non-empty diff whose file
paths cannot be derived is an error, never silently "no changes".
"""

from __future__ import annotations

import pytest
from agent_runtime.pathguard import DiffUnparseable, diff_files, filter_violations

_MODIFY = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1234567..89abcde 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
    "\\ No newline at end of file\n"
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
    # deletions are changed files too: old.txt must enter the guarded set
    assert diff_files(_MODIFY + _ADD + _DELETE) == ["src/app.py", "README.md", "old.txt"]


def test_diff_files_strips_timestamp() -> None:
    # some diff tools append a tab-separated timestamp to the header line
    assert diff_files("--- a/x.py\n+++ b/x.py\t2026-11-09 12:00:00 +0000\n") == ["x.py"]


def test_deletion_only_diff_is_guarded() -> None:
    changed = diff_files(_DELETE)
    assert changed == ["old.txt"]
    # outside the allow list (and under an empty one) a deletion is a violation
    assert filter_violations(changed, ["src/**"]) == ["old.txt"]
    assert filter_violations(changed, []) == ["old.txt"]
    # an explicitly allowed path is not
    assert filter_violations(changed, ["old.txt", "src/**"]) == []


def test_rename_yields_both_paths() -> None:
    diff = (
        "diff --git a/old_name.py b/new_name.py\n"
        "similarity index 90%\n"
        "rename from old_name.py\n"
        "rename to new_name.py\n"
        "--- a/old_name.py\n"
        "+++ b/new_name.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    assert diff_files(diff) == ["old_name.py", "new_name.py"]


@pytest.mark.parametrize(
    "diff",
    [
        "just some text\nnot a unified diff at all\n",
        "@@ -1 +1 @@\n-old\n+new\n",  # hunk body without any file header: not trusted
    ],
)
def test_unparseable_non_empty_diff_fails_closed(diff: str) -> None:
    # a non-empty diff we cannot derive paths from must never read as "no changes"
    with pytest.raises(DiffUnparseable):
        diff_files(diff)


def test_unparseable_diff_is_a_value_error() -> None:
    assert issubclass(DiffUnparseable, ValueError)


def test_empty_diff_means_no_changes() -> None:
    assert diff_files("") == []
    assert diff_files("  \n\n ") == []
    assert filter_violations([], []) == []  # genuinely no changes -> nothing to violate


def test_allow_violation_detected() -> None:
    changed = ["src/app.py", "docs/notes.md"]
    assert filter_violations(changed, ["src/**", "pyproject.toml"]) == ["docs/notes.md"]


def test_empty_allow_list_violates_everything() -> None:
    changed = ["src/app.py", "docs/notes.md"]
    assert filter_violations(changed, []) == changed


def test_glob_allows_subdir() -> None:
    assert filter_violations(["tests/unit/test_x.py"], ["tests/**"]) == []
    assert filter_violations(["src/wordtools/reverse.py"], ["src/wordtools/**"]) == []
