"""Bootstrapper seeds the check profile into a target repo (M5 design goal 2)."""

from __future__ import annotations

import pytest
from bootstrap.gitea import GiteaClient
from bootstrap.profiles import REQUIRED_CONTEXTS


class Recorder(GiteaClient):
    def __init__(
        self,
        responses: dict[tuple[str, str], int] | None = None,
        bodies: dict[tuple[str, str], dict[str, object]] | None = None,
    ) -> None:
        super().__init__(base_url="http://g", token="t", owner="o")
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.responses = responses or {}
        self.bodies = bodies or {}

    def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]:
        self.calls.append((method, path, payload))
        code = self.responses.get((method, path), 404 if method == "GET" else 201)
        return code, self.bodies.get((method, path), {})


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
        "/repos/o/demo/contents/.platform/project.yaml",
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
        lambda self, repo, *, stack, git_url, push_whitelist=(): seen.append(
            (repo, stack, git_url)
        ),
    )
    monkeypatch.setattr(cli, "register_or_update", lambda record: None)
    result = CliRunner().invoke(cli.app, ["onboard", "--project", "acme"])
    assert result.exit_code == 0, result.output
    assert seen == [("acme", "python", cli.PLATFORM_GIT_URL)]


def _b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode()).decode()


def test_identical_files_are_not_rewritten() -> None:
    from bootstrap.profiles import profile_files

    files = profile_files("platform")
    bodies: dict[tuple[str, str], dict[str, object]] = {
        ("GET", f"/repos/o/demo/contents/{p}"): {"sha": "s", "content": _b64(c)}
        for p, c in files.items()
    }
    responses = {key: 200 for key in bodies}
    rec = Recorder(responses, bodies)
    rec.apply_check_profile("demo", stack="platform", git_url="http://x")
    # Re-onboarding the platform repo creates no commits on main.
    assert not [p for m, p, _ in rec.calls if m in ("PUT", "POST") and "/contents/" in p]


def test_put_file_raises_on_http_error() -> None:
    rec = Recorder({("POST", "/repos/o/demo/contents/a.txt"): 403})
    with pytest.raises(RuntimeError, match="a.txt"):
        rec.put_file("demo", "a.txt", "x")


def test_push_whitelist_in_protection() -> None:
    rec = Recorder()
    rec.apply_check_profile(
        "demo", stack="python", git_url="http://x", push_whitelist=["swarm-admin"]
    )
    payload = next(p for m, path, p in rec.calls if path.endswith("/branch_protections"))
    assert payload is not None
    assert payload["enable_push"] is True
    assert payload["enable_push_whitelist"] is True
    assert payload["push_whitelist_usernames"] == ["swarm-admin"]


def test_default_protection_has_no_push() -> None:
    rec = Recorder()
    rec.apply_check_profile("demo", stack="python", git_url="http://x")
    payload = next(p for m, path, p in rec.calls if path.endswith("/branch_protections"))
    assert payload is not None and "enable_push" not in payload


def test_onboard_passes_push_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from bootstrap import cli

    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(cli, "_gitea_token", lambda: "t")
    monkeypatch.setattr(GiteaClient, "ensure_repo", lambda self, repo: "exists")
    monkeypatch.setattr(
        GiteaClient,
        "apply_check_profile",
        lambda self, repo, *, stack, git_url, push_whitelist=(): seen.append(tuple(push_whitelist)),
    )
    monkeypatch.setattr(cli, "register_or_update", lambda record: None)
    args = ["onboard", "--project", "p", "--stack", "platform", "--push-whitelist", "swarm-admin"]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert seen == [("swarm-admin",)]


def test_create_repo_in_org_uses_org_endpoint() -> None:
    # Agents (team `agents` in org swarm-lab) can only reach org repos.
    rec = Recorder({("POST", "/orgs/o/repos"): 201})
    assert rec.create_repo("demo") is True
    assert ("POST", "/orgs/o/repos") in [(m, p) for m, p, _ in rec.calls]
    assert "/user/repos" not in _paths(rec, "POST")


def test_create_repo_falls_back_to_user_when_owner_is_not_an_org() -> None:
    rec = Recorder({("POST", "/orgs/o/repos"): 404, ("POST", "/user/repos"): 201})
    assert rec.create_repo("demo") is True
    assert _paths(rec, "POST") == ["/orgs/o/repos", "/user/repos"]


def test_create_repo_existing_is_false() -> None:
    rec = Recorder({("POST", "/orgs/o/repos"): 409})
    assert rec.create_repo("demo") is False


def test_existing_protection_403_is_updated() -> None:
    # Gitea answers an existing protection with 403 "Branch protection already exist".
    rec = Recorder({("POST", "/repos/o/demo/branch_protections"): 403})
    rec.apply_check_profile("demo", stack="python", git_url="http://x")
    assert "/repos/o/demo/branch_protections/main" in _paths(rec, "PATCH")


def test_profile_overrides_replace_a_profile_file() -> None:
    rec = Recorder()
    rec.apply_check_profile(
        "demo",
        stack="python",
        git_url="http://gitea-http:3000",
        overrides={".platform/project.yaml": "intake: true\n"},
    )
    puts = [p for m, path, p in rec.calls if path.endswith("/contents/.platform/project.yaml")]
    assert len([p for p in puts if p]) == 1
    import base64

    last = puts[-1]
    assert last is not None
    assert base64.b64decode(str(last["content"])).decode() == "intake: true\n"


def test_demo_project_config_deploys_isv_product() -> None:
    from agent_runtime.project_config import parse_project_config
    from bootstrap.cli import DEMO_PROJECT_CONFIG

    cfg = parse_project_config(DEMO_PROJECT_CONFIG)
    assert cfg.intake is True
    assert cfg.deploy is not None and cfg.deploy.app == "isv-product"
    assert cfg.deploy.namespace == "prod"


def test_profile_ensures_the_intake_label() -> None:
    rec = Recorder()
    rec.apply_check_profile("demo", stack="python", git_url="http://gitea-http:3000")
    posts = [(path, p) for m, path, p in rec.calls if m == "POST" and path.endswith("/labels")]
    assert posts and posts[0][0] == "/repos/o/demo/labels"
    assert posts[0][1] is not None and posts[0][1]["name"] == "swarm"


def test_profile_keeps_an_existing_intake_label() -> None:
    rec = Recorder()
    rec.bodies[("GET", "/repos/o/demo/labels")] = [{"name": "swarm"}]  # type: ignore[assignment]
    rec.responses[("GET", "/repos/o/demo/labels")] = 200
    rec.apply_check_profile("demo", stack="python", git_url="http://gitea-http:3000")
    assert not [p for m, p, _ in rec.calls if m == "POST" and p.endswith("/labels")]


def test_protected_main_gets_one_proposal_pr() -> None:
    rec = Recorder(
        responses={
            ("POST", "/repos/o/demo/contents/.platform/project.yaml"): 403,
            ("GET", "/repos/o/demo/branches/platform/profile-update"): 404,
            ("POST", "/repos/o/demo/contents"): 201,
            ("POST", "/repos/o/demo/pulls"): 201,
        },
        bodies={("POST", "/repos/o/demo/pulls"): {"html_url": "http://g/o/demo/pulls/9"}},
    )
    url = rec.apply_check_profile("demo", stack="python", git_url="http://gitea-http:3000")
    assert url == "http://g/o/demo/pulls/9"
    multi = next(p for m, path, p in rec.calls if m == "POST" and path == "/repos/o/demo/contents")
    assert multi is not None
    assert multi["new_branch"] == "platform/profile-update"
    files = multi["files"]
    assert isinstance(files, list) and [f["path"] for f in files] == [".platform/project.yaml"]
    assert files[0]["operation"] == "create"
    # protection is still (re)applied
    assert any(path.endswith("/branch_protections") for _, path, _ in rec.calls)


def test_pending_proposal_is_not_duplicated() -> None:
    rec = Recorder(
        responses={
            ("POST", "/repos/o/demo/contents/.platform/project.yaml"): 403,
            ("GET", "/repos/o/demo/branches/platform/profile-update"): 200,
        }
    )
    url = rec.apply_check_profile("demo", stack="python", git_url="http://gitea-http:3000")
    assert url == "pending: platform/profile-update"
    assert not [p for m, p, _ in rec.calls if m == "POST" and p == "/repos/o/demo/pulls"]
