"""Per-stack check profiles seeded into target repos (spec §7.2 item 4, M5).

A profile is a set of repo files — the Gitea Actions CI corridor and the merge
policy — read from ``infra/templates/check-profiles/<stack>/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

PROFILES_DIR: Final[Path] = (
    Path(__file__).resolve().parents[4] / "infra" / "templates" / "check-profiles"
)

# Commit-status contexts Gitea Actions reports for the profile's jobs
# ("<workflow> / <job> (<event>)"); branch protection requires them.
CI_CONTEXT: Final[str] = "ci / checks (pull_request)"
SECURITY_CONTEXT: Final[str] = "ci / security (pull_request)"
REQUIRED_CONTEXTS: Final[tuple[str, ...]] = (CI_CONTEXT, SECURITY_CONTEXT)


def profile_files(stack: str) -> dict[str, str]:
    """Repo path → content of every file in the ``stack`` profile."""
    root = PROFILES_DIR / stack
    if not stack or "/" in stack or not root.is_dir():
        raise ValueError(f"unknown check profile {stack!r}")
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
