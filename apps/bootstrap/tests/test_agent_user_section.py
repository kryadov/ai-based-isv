"""Agents use a non-admin Gitea identity (self-dev level 2, design §3)."""

from __future__ import annotations

import re
from pathlib import Path

BOOTSTRAP = Path(__file__).resolve().parents[3] / "infra" / "scripts" / "bootstrap.sh"


def _text() -> str:
    return BOOTSTRAP.read_text(encoding="utf-8")


def test_section_creates_non_admin_agent_user_with_org_write_team() -> None:
    text = _text()
    body = text[text.index("section_agent_user() {") :]
    body = body[: body.index("\n}\n")]
    for needle in (
        "secret/gitea/agent",
        "/admin/users",
        "swarm-agent",
        "includes_all_repositories",
        '"permission":"write"',
        "swarm-lab",
    ):
        assert needle in body, needle
    assert '"admin":true' not in body.replace(" ", "")


def test_section_is_dispatchable_and_runs_after_gitea() -> None:
    text = _text()
    assert "agent-user) section_agent_user" in text
    all_line = next(line for line in text.splitlines() if re.match(r"\s*all\)", line))
    assert all_line.index("section_gitea") < all_line.index("section_agent_user")
