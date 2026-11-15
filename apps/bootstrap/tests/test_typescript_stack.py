"""Polyglot dogfood (M9): the typescript check profile and the ts-service template."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from bootstrap.profiles import profile_files
from bootstrap.templates import TEMPLATES_DIR, template_tree


def _workflow() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(profile_files("typescript")[".gitea/workflows/ci.yml"])
    return data


def _runs(job: str) -> str:
    return "\n".join(str(s.get("run", "")) for s in _workflow()["jobs"][job]["steps"])


def test_typescript_profile_gates() -> None:
    wf = _workflow()
    assert wf["name"] == "ci" and set(wf["jobs"]) == {"checks", "security"}
    checks = _runs("checks")
    order = [checks.index(s) for s in ("npm ci", "npx tsc --noEmit", "npx eslint .", "npm test")]
    assert order == sorted(order)
    python_security = yaml.safe_load(profile_files("python")[".gitea/workflows/ci.yml"])
    assert wf["jobs"]["security"] == python_security["jobs"]["security"]


def test_ts_service_template_tree(tmp_path: Path) -> None:
    paths = template_tree("ts-service", tmp_path)
    for expected in (
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "eslint.config.mjs",
        ".gitignore",
        "src/server.ts",
        "src/greet.ts",
        "test/greet.test.ts",
    ):
        assert expected in paths, expected
    assert not [p for p in paths if p.startswith(("node_modules/", "dist/"))]
    ignore = (tmp_path / ".gitignore").read_text(encoding="utf-8").split()
    assert "node_modules/" in ignore and "dist/" in ignore


def test_ts_service_scripts_and_lock_match() -> None:
    root = TEMPLATES_DIR / "ts-service"
    pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert {"build", "test", "lint"} <= set(pkg["scripts"])
    assert "node --test" in pkg["scripts"]["test"]
    lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"][""]["devDependencies"] == pkg["devDependencies"]


def test_unknown_template_is_rejected(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown template"):
        template_tree("../etc", tmp_path)


def _onboard(monkeypatch: Any, status: str, *extra: str) -> tuple[Any, list[str]]:
    import pytest  # noqa: F401
    from bootstrap.gitea import GiteaClient
    from typer.testing import CliRunner

    from bootstrap import cli

    events: list[str] = []
    monkeypatch.setattr(cli, "_gitea_token", lambda: "t")
    monkeypatch.setattr(GiteaClient, "ensure_repo", lambda self, repo: status)
    monkeypatch.setattr(
        GiteaClient,
        "apply_check_profile",
        lambda self, repo, *, stack, git_url, push_whitelist=(): events.append(f"profile:{stack}"),
    )

    def fake_push(url: str, token: str, workdir: Path, *, message: str, build: Any = None) -> None:
        paths = build(workdir / "probe") if build else []
        events.append(f"push:{url}:{'package.json' in paths}")

    monkeypatch.setattr(cli, "push_seed", fake_push)
    monkeypatch.setattr(cli, "register_or_update", lambda record: None)
    args = ["onboard", "--project", "ts-greeter", "--stack", "typescript", *extra]
    return CliRunner().invoke(cli.app, args), events


def test_onboard_with_template_seeds_before_profile(monkeypatch: Any) -> None:
    result, events = _onboard(monkeypatch, "created", "--template", "ts-service")
    assert result.exit_code == 0, result.output
    assert events == [
        "push:http://localhost:30080/swarm-lab/ts-greeter.git:True",
        "profile:typescript",
    ]


def test_onboard_existing_repo_is_not_reseeded(monkeypatch: Any) -> None:
    result, events = _onboard(monkeypatch, "exists", "--template", "ts-service")
    assert result.exit_code == 0 and events == ["profile:typescript"]


def test_onboard_unknown_template_exits(monkeypatch: Any) -> None:
    result, events = _onboard(monkeypatch, "created", "--template", "nope")
    assert result.exit_code == 1 and events == []
