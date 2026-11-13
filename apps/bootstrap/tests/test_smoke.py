from pathlib import Path

import pytest
from bootstrap.cli import app
from bootstrap.prereq import check_all
from typer.testing import CliRunner

from bootstrap import __version__

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_check_reports_known_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SWARM_CLUSTER", raising=False)
    result = runner.invoke(app, ["check"])
    output = result.output
    for tool in ("docker", "k3d", "helm", "kubectl", "uv", "gh", "ollama", "psql"):
        assert tool in output


def test_check_all_returns_results() -> None:
    results = check_all(("docker",))
    assert results[0].name == "docker"
    assert isinstance(results[0].found, bool)


def test_py_typed_marker_present() -> None:
    import bootstrap

    marker = Path(bootstrap.__file__).parent / "py.typed"
    assert marker.exists()


def test_required_tools_k3d_flavour_needs_k3d() -> None:
    from bootstrap.prereq import required_tools

    assert "k3d" in required_tools("k3d")


def test_required_tools_rancher_desktop_drops_k3d() -> None:
    from bootstrap.prereq import required_tools

    tools = required_tools("rancher-desktop")
    assert "k3d" not in tools
    for tool in ("docker", "helm", "kubectl", "uv", "gh", "ollama", "psql"):
        assert tool in tools


def test_check_follows_swarm_cluster_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_CLUSTER", "rancher-desktop")
    result = runner.invoke(app, ["check"])
    assert " k3d " not in result.output
