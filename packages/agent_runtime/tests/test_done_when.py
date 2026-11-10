"""Tests for the code-validated done_when checker — the fail-closed acceptance core.

After the agent finishes, our code (never an LLM) runs the vertex's declarative
checks. Every ambiguous input must FAIL: unparseable specs raise
``DoneWhenUnparseable``, unknown or incomplete checks give a ``passed=False``
verdict. Nothing ever passes silently.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import agent_runtime.done_when as done_when_module
import pytest
from agent_runtime.done_when import (
    DoneWhenCheck,
    DoneWhenError,
    DoneWhenUnparseable,
    parse_done_when,
    validate_done_when,
)


def test_parse_valid() -> None:
    raw = (
        '{"checks": [{"type": "cmd", "run": "pytest -q"}, '
        '{"type": "files_exist", "paths": ["src/app.py"]}]}'
    )

    spec = parse_done_when(raw)

    assert [check.type for check in spec.checks] == ["cmd", "files_exist"]
    assert spec.checks[0] == DoneWhenCheck(type="cmd", run="pytest -q")
    assert spec.checks[1] == DoneWhenCheck(type="files_exist", paths=["src/app.py"])


@pytest.mark.parametrize(
    "raw",
    [
        "",  # nothing at all
        "   ",  # whitespace only
        "not json",  # garbage
        "[]",  # valid JSON, but no "checks" key
        '{"checks": []}',  # "checks" empty: the vertex would pass without any evidence
        '{"checks": [{"type": 1}]}',  # wrong shape of a check
    ],
)
def test_parse_empty_is_unparseable(raw: str) -> None:
    with pytest.raises(DoneWhenUnparseable):
        parse_done_when(raw)


def test_unparseable_is_a_done_when_error() -> None:
    assert issubclass(DoneWhenUnparseable, DoneWhenError)
    assert issubclass(DoneWhenError, ValueError)


def test_cmd_pass_and_fail(tmp_path: Path) -> None:
    seen: list[tuple[str, Path]] = []

    def fake_run_cmd(cmd: str, cwd: Path) -> int:
        seen.append((cmd, cwd))
        return 0 if cmd == "pytest -q" else 1

    passing = parse_done_when('{"checks": [{"type": "cmd", "run": "pytest -q"}]}')
    verdict = validate_done_when(passing, cwd=tmp_path, run_cmd=fake_run_cmd)
    assert verdict.passed is True
    assert verdict.reason is None
    assert len(verdict.results) == 1
    assert verdict.results[0].passed is True
    assert seen == [("pytest -q", tmp_path)]

    failing = parse_done_when('{"checks": [{"type": "cmd", "run": "mypy --strict"}]}')
    verdict = validate_done_when(failing, cwd=tmp_path, run_cmd=fake_run_cmd)
    assert verdict.passed is False
    assert verdict.reason is not None
    assert [result.passed for result in verdict.results] == [False]


def test_default_cmd_runner_does_not_leak_worker_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_RUNTIME_GIT_TOKEN", "secret")
    monkeypatch.setenv("LITELLM_API_KEY", "also-secret")
    output_path = tmp_path / "child-env.json"
    script = (
        "import json, os, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(os.environ)), encoding='utf-8')"
    )
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} {shlex.quote(str(output_path))}"
    )
    spec = parse_done_when(json.dumps({"checks": [{"type": "cmd", "run": command}]}))

    verdict = validate_done_when(spec, cwd=tmp_path)

    assert verdict.passed is True
    child_env = json.loads(output_path.read_text(encoding="utf-8"))
    assert "AGENT_RUNTIME_GIT_TOKEN" not in child_env
    assert "LITELLM_API_KEY" not in child_env
    assert "PATH" in child_env


def test_default_cmd_runner_supports_pytest_with_scrubbed_environment(
    tmp_path: Path,
) -> None:
    (tmp_path / "test_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    spec = parse_done_when('{"checks": [{"type": "cmd", "run": "pytest -q"}]}')

    verdict = validate_done_when(spec, cwd=tmp_path)

    assert verdict.passed is True, verdict.reason


def test_default_cmd_runner_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(done_when_module, "_CMD_TIMEOUT_SECONDS", 0.05, raising=False)
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(1)')}"
    spec = parse_done_when(json.dumps({"checks": [{"type": "cmd", "run": command}]}))

    verdict = validate_done_when(spec, cwd=tmp_path)

    assert verdict.passed is False
    assert verdict.reason is not None
    assert "timed out" in verdict.reason


def test_first_fail_stops_execution(tmp_path: Path) -> None:
    seen: list[str] = []

    def fake_run_cmd(cmd: str, cwd: Path) -> int:
        seen.append(cmd)
        return 1 if cmd == "first" else 0

    spec = parse_done_when(
        '{"checks": [{"type": "cmd", "run": "first"}, {"type": "cmd", "run": "second"}]}'
    )
    verdict = validate_done_when(spec, cwd=tmp_path, run_cmd=fake_run_cmd)

    assert verdict.passed is False
    assert seen == ["first"]  # checks run in spec order; the first FAIL stops the rest
    assert len(verdict.results) == 1


def test_files_exist_pass_and_fail(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    passing = parse_done_when('{"checks": [{"type": "files_exist", "paths": ["src/app.py"]}]}')
    verdict = validate_done_when(passing, cwd=tmp_path)
    assert verdict.passed is True
    assert verdict.results[0].passed is True

    failing = parse_done_when(
        '{"checks": [{"type": "files_exist", "paths": ["src/app.py", "src/missing.py"]}]}'
    )
    verdict = validate_done_when(failing, cwd=tmp_path)
    assert verdict.passed is False
    assert "src/missing.py" in verdict.results[0].detail


def test_files_exist_rejects_absolute_paths_and_parent_escapes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    spec = parse_done_when(
        json.dumps(
            {
                "checks": [
                    {
                        "type": "files_exist",
                        "paths": [str(outside), "../secret.txt", "src/../../secret.txt"],
                    }
                ]
            }
        )
    )

    verdict = validate_done_when(spec, cwd=repo)

    assert verdict.passed is False
    assert verdict.results[0].passed is False
    assert "outside the working directory" in verdict.results[0].detail


def test_unknown_check_type_fails_closed(tmp_path: Path) -> None:
    def never(cmd: str, cwd: Path) -> int:
        raise AssertionError("no cmd check in this spec")

    spec = parse_done_when('{"checks": [{"type": "regex", "run": "nothing"}]}')
    verdict = validate_done_when(spec, cwd=tmp_path, run_cmd=never)

    assert verdict.passed is False
    assert verdict.reason == "unknown check type: regex"
    assert verdict.results[0].check.type == "regex"
    assert verdict.results[0].passed is False


def test_incomplete_checks_fail_closed(tmp_path: Path) -> None:
    def boom(cmd: str, cwd: Path) -> int:
        raise AssertionError("a cmd check without 'run' must not execute anything")

    no_run = parse_done_when('{"checks": [{"type": "cmd"}]}')
    verdict = validate_done_when(no_run, cwd=tmp_path, run_cmd=boom)
    assert verdict.passed is False

    no_paths = parse_done_when('{"checks": [{"type": "files_exist", "paths": []}]}')
    verdict = validate_done_when(no_paths, cwd=tmp_path)
    assert verdict.passed is False
