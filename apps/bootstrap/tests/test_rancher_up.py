"""infra/scripts/rancher-up.sh against a fake kubectl (Rancher Desktop stand check)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra" / "scripts" / "rancher-up.sh"

FAKE_KUBECTL = """#!/usr/bin/env bash
case "$*" in
  "config current-context") echo rancher-desktop ;;
  wait*) exit 0 ;;
  *containerRuntimeVersion*) echo docker://28.0.0 ;;
  "create service nodeport"*) [ -n "$PROBE_ERR" ] && { echo "$PROBE_ERR" >&2; exit 1; }; exit 0 ;;
  "get nodes -o wide") echo node ;;
  *) exit 0 ;;
esac
"""

bash = shutil.which("bash")
pytestmark = pytest.mark.skipif(bash is None, reason="needs bash")


def _run(tmp_path: Path, probe_err: str) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "kubectl"
    fake.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    path = f"{tmp_path}{os.pathsep}{os.environ['PATH']}"
    env = {**os.environ, "PATH": path, "PROBE_ERR": probe_err}
    return subprocess.run(  # noqa: S603
        [str(bash), str(SCRIPT)], capture_output=True, text=True, env=env, check=False, timeout=60
    )


def test_port_free_and_in_range_passes(tmp_path: Path) -> None:
    assert _run(tmp_path, "").returncode == 0


def test_port_already_allocated_means_the_range_is_fine(tmp_path: Path) -> None:
    # The live stand's Postgres NodePort already holds 15432 (m2d re-apply).
    err = (
        'The Service "swarm-range-probe" is invalid: spec.ports[0].nodePort: '
        "Invalid value: 15432: provided port is already allocated"
    )
    result = _run(tmp_path, err)
    assert result.returncode == 0, result.stderr


def test_port_outside_the_range_fails_with_a_hint(tmp_path: Path) -> None:
    err = (
        'The Service "swarm-range-probe" is invalid: spec.ports[0].nodePort: '
        "Invalid value: 15432: provided port is not in the valid range. "
        "The range of valid ports is 30000-32767"
    )
    result = _run(tmp_path, err)
    assert result.returncode == 1
    assert "swarm-lab.start" in result.stderr
