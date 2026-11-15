"""Org memory repo (M7, spec §7.4, ADR-0003): seed tree and promoted lessons.

``swarm-lab/org-memory`` holds organization semantic memory: lessons promoted
from project episodic memory (human-reviewed PRs only in v0) and the reuse
catalog v1. Its CI is the ``docs`` check profile.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Final

import yaml

from bootstrap.profiles import profile_files

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
TEMPLATE_DIR: Final[Path] = REPO_ROOT / "infra" / "templates" / "org-memory"
ORG_MEMORY_REPO: Final[str] = "org-memory"


def seed_tree(dest: Path) -> list[str]:
    """Assemble the org-memory repo into ``dest``; return its sorted relative paths."""
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_DIR, dest, dirs_exist_ok=True, copy_function=shutil.copyfile)
    for path, content in profile_files("docs").items():
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return sorted(
        p.relative_to(dest).as_posix()
        for p in dest.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(dest).parts
    )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def lesson_path(episode: dict[str, Any]) -> str:
    return f"lessons/{_slug(str(episode['project']))}-{episode['id']}.md"


def lesson_markdown(episode: dict[str, Any]) -> str:
    """The promoted lesson: provenance front matter + the episode body."""
    meta = {
        "title": episode["title"],
        "kind": episode["kind"],
        "project": episode["project"],
        "source": {
            "episode": episode["id"],
            "run": episode.get("run_id"),
            "vertex": episode.get("vertex_id"),
        },
        "paths": list(episode.get("paths") or []),
        "recorded": episode.get("created_at"),
    }
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    return f"---\n{front}---\n\n# {episode['title']}\n\n{episode['body'].strip()}\n"
