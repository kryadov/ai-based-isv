"""Tests for `prereq._version_of`: the version probe must fail closed.

A `--version` that exits non-zero means the binary is present but unusable
(broken or a stub), so `_version_of` must return None instead of trusting
its output — `check_all` then reports the tool as NOT FOUND.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from bootstrap import prereq


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """A canned `subprocess.run` result for a `--version` probe."""
    return subprocess.CompletedProcess(
        args=["/usr/bin/tool", "--version"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class _FakeRun:
    """Stands in for `subprocess.run`: records argv, returns a canned outcome."""

    def __init__(self, outcome: subprocess.CompletedProcess[str] | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _patch_probe(
    monkeypatch: pytest.MonkeyPatch, run: _FakeRun, exe: str | None = "/usr/bin/tool"
) -> None:
    monkeypatch.setattr(prereq.shutil, "which", lambda _binary: exe)
    monkeypatch.setattr(prereq.subprocess, "run", run)


def test_success_returns_first_stdout_line(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _FakeRun(_completed(0, stdout="docker version 27.3.1\nbuild 66b3b16\n"))
    _patch_probe(monkeypatch, run)
    assert prereq._version_of("docker") == "docker version 27.3.1"
    assert run.calls == [["/usr/bin/tool", "--version"]]


def test_success_falls_back_to_stderr_when_stdout_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _FakeRun(_completed(0, stderr="helm v3.16.1\n"))
    _patch_probe(monkeypatch, run)
    assert prereq._version_of("helm") == "helm v3.16.1"


def test_success_with_no_output_reports_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _FakeRun(_completed(0))
    _patch_probe(monkeypatch, run)
    assert prereq._version_of("gh") == "installed"


@pytest.mark.parametrize("returncode", [1, 2, 127])
def test_nonzero_exit_returns_none_even_with_output(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    run = _FakeRun(_completed(returncode, stdout="docker version 27.3.1\n", stderr="boom\n"))
    _patch_probe(monkeypatch, run)
    assert prereq._version_of("docker") is None


def test_nonzero_exit_with_stderr_only_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _FakeRun(_completed(3, stderr="error: unknown flag\n"))
    _patch_probe(monkeypatch, run)
    assert prereq._version_of("kubectl") is None


def test_missing_binary_returns_none_without_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _FakeRun(_completed(0, stdout="should never run\n"))
    _patch_probe(monkeypatch, run, exe=None)
    assert prereq._version_of("nope") is None
    assert run.calls == []


@pytest.mark.parametrize(
    "exc",
    [
        OSError("cannot execute"),
        subprocess.TimeoutExpired(cmd=["/usr/bin/tool", "--version"], timeout=15),
    ],
)
def test_probe_failure_returns_none(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    run = _FakeRun(exc)
    _patch_probe(monkeypatch, run)
    assert prereq._version_of("uv") is None


def test_check_all_reports_nonzero_exit_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    run = _FakeRun(_completed(1, stdout="docker version 27.3.1\n"))
    _patch_probe(monkeypatch, run)
    (result,) = prereq.check_all(("docker",))
    assert result == prereq.CheckResult(name="docker", found=False, detail="NOT FOUND")
