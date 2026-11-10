"""Code-validated ``done_when`` checks — the fail-closed acceptance core of a vertex.

After the agent reports done, OUR code (never an LLM) executes the declarative
checks attached to the vertex: a ``cmd`` check must exit 0, a ``files_exist``
check's paths must be present under the repo working directory. Checks run in
specification order and the first FAIL stops the evaluation.

In M4a ``cmd`` is the host-side boundary: it runs agent-authored shell code with a
scrubbed environment (``PATH``/``HOME``/``LANG``/``TMPDIR`` only) and a timeout. The
host filesystem remains reachable by design; M4b moves these checks into the pod.

Fail-closed policy (spec 2026-11-09-m4-agent-runtime-design.md, §done_when):

* a raw string that is empty, unparseable, or has a missing/empty/wrong-shaped
  ``checks`` raises :class:`DoneWhenUnparseable` — the worker turns that into a
  FAIL verdict with reason ``unparseable_done_when``;
* an unknown check type, or a known type with a missing/mandatory argument
  absent, is not an error but a failed check (``passed=False``): ambiguous input
  can never let the vertex pass silently.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, ValidationError


class DoneWhenError(ValueError):
    """Base class for done_when handling failures."""


class DoneWhenUnparseable(DoneWhenError):
    """The raw done_when string is not a usable checks spec (empty or broken)."""


class DoneWhenCheck(BaseModel):
    """One declarative check: ``cmd`` carries ``run``, ``files_exist`` carries ``paths``."""

    type: str
    run: str | None = None
    paths: list[str] | None = None


class DoneWhenSpec(BaseModel):
    """Parsed checks of a vertex. An empty checks list is rejected: with zero
    checks the vertex would pass without any evidence (fail-closed)."""

    checks: Annotated[list[DoneWhenCheck], Field(min_length=1)]


class CheckResult(BaseModel):
    """Outcome of one executed check."""

    check: DoneWhenCheck
    passed: bool
    detail: str


class DoneWhenVerdict(BaseModel):
    """Overall acceptance verdict; ``results`` holds the checks executed up to
    and including the first FAIL."""

    passed: bool
    reason: str | None = None
    results: list[CheckResult]


_CMD_TYPE = "cmd"
_FILES_EXIST_TYPE = "files_exist"
_CMD_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "TMPDIR")
_CMD_TIMEOUT_SECONDS = 300.0


def parse_done_when(raw: str) -> DoneWhenSpec:
    """Parse the vertex's done_when string into a spec; anything unusable raises
    :class:`DoneWhenUnparseable`."""
    text = raw.strip()
    if not text:
        raise DoneWhenUnparseable("done_when is empty")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DoneWhenUnparseable(f"done_when is not valid JSON: {exc}") from exc
    try:
        return DoneWhenSpec.model_validate(data)
    except ValidationError as exc:
        raise DoneWhenUnparseable(f"done_when has no usable 'checks': {exc}") from exc


def _cmd_environment() -> dict[str, str]:
    """Return only the non-secret variables needed by ordinary build tools."""
    return {key: os.environ[key] for key in _CMD_ENV_ALLOWLIST if key in os.environ}


def _subprocess_run_cmd(cmd: str, cwd: Path) -> int:
    """Default runner: bounded shell execution without the worker's secret env."""
    # shell=True is intentional: check bodies may use shell composition (pipes, &&).
    # M4a reduces host exposure through this environment boundary; M4b uses a pod.
    completed = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        env=_cmd_environment(),
        capture_output=True,
        check=False,
        timeout=_CMD_TIMEOUT_SECONDS,
    )
    return completed.returncode


def _path_is_within_cwd(raw_path: str, cwd: Path) -> bool:
    """Accept relative paths only, including symlink-aware containment."""
    path = Path(raw_path)
    return not path.is_absolute() and (cwd / path).resolve().is_relative_to(cwd)


def _run_one_check(
    check: DoneWhenCheck,
    *,
    cwd: Path,
    runner: Callable[[str, Path], int],
) -> tuple[bool, str]:
    if check.type == _CMD_TYPE:
        if not check.run:
            return False, "cmd check has no 'run' command"
        try:
            code = runner(check.run, cwd)
        except Exception as exc:  # a runner blow-up is a failed check, not a crash
            return False, f"command {check.run!r} could not run: {exc}"
        if code == 0:
            return True, f"command {check.run!r} exited 0"
        return False, f"command {check.run!r} exited {code}"
    if check.type == _FILES_EXIST_TYPE:
        if not check.paths:
            return False, "files_exist check has no 'paths'"
        resolved_cwd = cwd.resolve()
        unsafe = [path for path in check.paths if not _path_is_within_cwd(path, resolved_cwd)]
        if unsafe:
            return False, f"paths outside the working directory: {', '.join(unsafe)}"
        missing = [path for path in check.paths if not (resolved_cwd / path).exists()]
        if missing:
            return False, f"missing paths: {', '.join(missing)}"
        return True, f"all {len(check.paths)} paths exist under {cwd}"
    return False, f"unknown check type: {check.type}"


def validate_done_when(
    spec: DoneWhenSpec,
    *,
    cwd: Path,
    run_cmd: Callable[[str, Path], int] | None = None,
) -> DoneWhenVerdict:
    """Execute the checks in spec order; the first FAIL stops and fails the verdict."""
    runner = run_cmd if run_cmd is not None else _subprocess_run_cmd
    results: list[CheckResult] = []
    for check in spec.checks:
        passed, detail = _run_one_check(check, cwd=cwd, runner=runner)
        results.append(CheckResult(check=check, passed=passed, detail=detail))
        if not passed:
            return DoneWhenVerdict(passed=False, reason=detail, results=results)
    return DoneWhenVerdict(passed=True, reason=None, results=results)
