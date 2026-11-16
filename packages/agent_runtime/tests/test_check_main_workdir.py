"""The final judge clones main under a docker-visible workdir (M11): a TS project's
done_when runs `docker run -v "$PWD":/w`, and the daemon container's /tmp is not
visible to the docker daemon."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_runtime import swarm


async def test_check_main_clones_under_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen["dest"] = Path(argv[-1])
        Path(argv[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        swarm,
        "validate_done_when",
        lambda spec, cwd: type("V", (), {"passed": True, "reason": None})(),
    )
    check = swarm._check_main_factory("http://g/o/r.git", "main", "true", None, workdir=tmp_path)
    ok, _ = await check()
    assert ok
    assert tmp_path in seen["dest"].parents
