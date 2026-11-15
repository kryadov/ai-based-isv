"""Project templates (spec §13.1.5, M9): code skeletons per stack/kind.

``infra/templates/projects/<name>/`` holds a project's starting code; the CI
corridor comes separately from the check profile of its stack. Build output
and dependency folders never travel with a template.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Final

TEMPLATES_DIR: Final[Path] = (
    Path(__file__).resolve().parents[4] / "infra" / "templates" / "projects"
)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SKIP = frozenset({"node_modules", "dist", "__pycache__", ".venv", "target"})


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in _SKIP}


def template_tree(name: str, dest: Path) -> list[str]:
    """Copy template ``name`` into ``dest``; return its sorted relative paths."""
    root = TEMPLATES_DIR / name
    if not _NAME_RE.match(name) or not root.is_dir():
        raise ValueError(f"unknown template {name!r}")
    shutil.copytree(root, dest, ignore=_ignore, dirs_exist_ok=True, copy_function=shutil.copyfile)
    return sorted(
        p.relative_to(dest).as_posix()
        for p in dest.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(dest).parts
    )
