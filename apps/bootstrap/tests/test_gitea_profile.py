"""Bootstrapper seeds the check profile into a target repo (M5 design goal 2)."""

from __future__ import annotations

import pytest
from bootstrap.gitea import GiteaClient
from bootstrap.profiles import REQUIRED_CONTEXTS


class Recorder(GiteaClient):
    def __init__(self, responses: dict[tuple[str, str], int] | None = None) -> None:
        super().__init__(base_url="http://g", token="t", owner="o")
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.responses = responses or {}

    def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]:
        self.calls.append((method, path, payload))
        return self.responses.get((method, path), 404 if method == "GET" else 201), {}


def _paths(rec: Recorder, method: str) -> list[str]:
    return [p for m, p, _ in rec.calls if m == method]


def test_profile_files_then_variable_then_protection() -> None:
    rec = Recorder()
    rec.apply_check_profile("demo", stack="python", git_url="http://gitea-http:3000")
    writes = [(m, p) for m, p, _ in rec.calls if m != "GET"]
    files = [p for m, p in writes if "/contents/" in p]
    assert files == [
        "/repos/o/demo/contents/.gitea/workflows/ci.yml",
        "/repos/o/demo/contents/.platform/merge-policy.yaml",
    ]
    order = [p for _, p in writes]
    var = order.index("/repos/o/demo/actions/variables/PLATFORM_GIT_URL")
    prot = order.index("/repos/o/demo/branch_protections")
    # Protection last: afterwards direct commits to main are rejected.
    assert max(order.index(f) for f in files) < var < prot
    payload = next(p for m, path, p in rec.calls if path.endswith("/branch_protections"))
    assert payload == {
        "branch_name": "main",
        "enable_status_check": True,
        "status_check_contexts": list(REQUIRED_CONTEXTS),
    }


def test_existing_variable_and_protection_are_updated() -> None:
    rec = Recorder(
        {
            ("POST", "/repos/o/demo/actions/variables/PLATFORM_GIT_URL"): 409,
            ("POST", "/repos/o/demo/branch_protections"): 422,
        }
    )
    rec.apply_check_profile("demo", stack="python", git_url="http://x")
    assert ("PUT", "/repos/o/demo/actions/variables/PLATFORM_GIT_URL") in [
        (m, p) for m, p, _ in rec.calls
    ]
    assert "/repos/o/demo/branch_protections/main" in _paths(rec, "PATCH")


def test_failed_protection_raises() -> None:
    rec = Recorder({("POST", "/repos/o/demo/branch_protections"): 500})
    with pytest.raises(RuntimeError):
        rec.apply_check_profile("demo", stack="python", git_url="http://x")


def test_onboard_seeds_the_check_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    from typer.testing import CliRunner

    from bootstrap import cli

    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(cli, "_gitea_token", lambda: "t")
    monkeypatch.setattr(GiteaClient, "ensure_repo", lambda self, repo: "created")
    monkeypatch.setattr(
        GiteaClient,
        "apply_check_profile",
        lambda self, repo, *, stack, git_url: seen.append((repo, stack, git_url)),
    )
    monkeypatch.setattr(cli, "register_or_update", lambda record: None)
    result = CliRunner().invoke(cli.app, ["onboard", "--project", "acme"])
    assert result.exit_code == 0, result.output
    assert seen == [("acme", "python", cli.PLATFORM_GIT_URL)]
