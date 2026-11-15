"""Org memory repo (M7, ADR-0003): seed, reuse catalog v1, lesson promotion PRs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from bootstrap.org_memory import lesson_markdown, lesson_path, seed_tree
from bootstrap.profiles import profile_files

EPISODE: dict[str, Any] = {
    "id": 41,
    "project": "swarm-lab/isv-product",
    "kind": "postmortem",
    "title": "canary isv-product:abc rolled back",
    "body": "# Postmortem (draft)\n\n- error-ratio failed",
    "paths": ["src/isv_product/main.py"],
    "run_id": 3,
    "vertex_id": 9,
    "created_at": "2026-11-16T10:00:00+00:00",
}


def test_seed_tree_has_catalog_lessons_and_docs_profile(tmp_path: Path) -> None:
    paths = seed_tree(tmp_path)
    for expected in (
        "README.md",
        "reuse-catalog.yaml",
        "lessons/.gitkeep",
        ".gitea/workflows/ci.yml",
        ".platform/merge-policy.yaml",
    ):
        assert expected in paths, expected


def test_reuse_catalog_entries_are_complete(tmp_path: Path) -> None:
    seed_tree(tmp_path)
    catalog = yaml.safe_load((tmp_path / "reuse-catalog.yaml").read_text(encoding="utf-8"))
    entries = catalog["components"]
    assert len(entries) >= 3
    for entry in entries:
        assert set(entry) >= {"name", "kind", "repo", "path", "description"}, entry
    assert {e["name"] for e in entries} >= {
        "check-profile-python",
        "deploy-canary-service",
        "isv-product-service-template",
    }


def test_docs_profile_has_both_required_contexts() -> None:
    workflow = yaml.safe_load(profile_files("docs")[".gitea/workflows/ci.yml"])
    assert workflow["name"] == "ci" and set(workflow["jobs"]) == {"checks", "security"}
    checks = "\n".join(str(s.get("run", "")) for s in workflow["jobs"]["checks"]["steps"])
    assert "yaml.safe_load" in checks


def test_lesson_markdown_front_matter() -> None:
    text = lesson_markdown(EPISODE)
    head, _, body = text.partition("\n---\n")
    meta = yaml.safe_load(head.removeprefix("---\n"))
    assert meta == {
        "title": EPISODE["title"],
        "kind": "postmortem",
        "project": "swarm-lab/isv-product",
        "source": {"episode": 41, "run": 3, "vertex": 9},
        "paths": ["src/isv_product/main.py"],
        "recorded": "2026-11-16T10:00:00+00:00",
    }
    assert "error-ratio failed" in body


def test_lesson_path_is_slugged() -> None:
    assert lesson_path(EPISODE) == "lessons/swarm-lab-isv-product-41.md"


def test_promote_unknown_episode_exits_without_gitea(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from bootstrap import cli

    monkeypatch.setattr(cli, "_fetch_episode", lambda api, eid: None)
    monkeypatch.setattr(cli, "_gitea_token", lambda: pytest.fail("Gitea must not be touched"))
    result = CliRunner().invoke(cli.app, ["memory", "promote", "--episode", "999"])
    assert result.exit_code == 1
    assert "999" in result.output


def test_promote_opens_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    from bootstrap.gitea import GiteaClient
    from typer.testing import CliRunner

    from bootstrap import cli

    proposals: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(cli, "_fetch_episode", lambda api, eid: EPISODE)
    monkeypatch.setattr(cli, "_gitea_token", lambda: "t")

    def propose(self: GiteaClient, repo: str, path: str, content: str, *, branch: str,
                title: str, body: str) -> str:  # fmt: skip
        proposals.append((repo, path, branch, title))
        return "http://g/swarm-lab/org-memory/pulls/1"

    monkeypatch.setattr(GiteaClient, "propose_file", propose)
    result = CliRunner().invoke(cli.app, ["memory", "promote", "--episode", "41"])
    assert result.exit_code == 0, result.output
    assert proposals == [
        (
            "org-memory",
            "lessons/swarm-lab-isv-product-41.md",
            "lesson/swarm-lab-isv-product-41",
            "lesson: canary isv-product:abc rolled back",
        )
    ]
    assert "pulls/1" in result.output


def test_propose_file_creates_branch_then_pr() -> None:
    from bootstrap.gitea import GiteaClient

    calls: list[tuple[str, str, dict[str, object] | None]] = []

    class Rec(GiteaClient):
        def _request(
            self, method: str, path: str, payload: dict[str, object] | None = None
        ) -> tuple[int, dict[str, object]]:
            calls.append((method, path, payload))
            if path.endswith("/pulls"):
                return 201, {"html_url": "http://g/o/r/pulls/2"}
            return 201, {}

    url = Rec("http://g", "t", "o").propose_file(
        "r", "lessons/x.md", "hello", branch="lesson/x", title="lesson: x", body="b"
    )
    assert url == "http://g/o/r/pulls/2"
    assert calls[0][0] == "POST" and calls[0][1] == "/repos/o/r/contents/lessons/x.md"
    assert calls[0][2] is not None and calls[0][2]["new_branch"] == "lesson/x"
    assert calls[1][1] == "/repos/o/r/pulls"
    assert calls[1][2] == {"title": "lesson: x", "head": "lesson/x", "base": "main", "body": "b"}
