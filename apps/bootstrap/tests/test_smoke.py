from pathlib import Path

from bootstrap.cli import app
from bootstrap.prereq import check_all
from typer.testing import CliRunner

from bootstrap import __version__

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_check_reports_known_tools() -> None:
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
