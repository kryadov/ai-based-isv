"""Prerequisites detection for the organization bootstrapper.

Checks the presence of L0-stand tools and returns a structured
report (ok/missing + path/version). No side effects.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Final

CHECKS: Final[tuple[str, ...]] = ("docker", "k3d", "helm", "kubectl", "uv", "gh", "ollama", "psql")


def required_tools(cluster: str) -> tuple[str, ...]:
    """Tools the stand needs for cluster flavour `cluster` (SWARM_CLUSTER).

    Rancher Desktop owns its k3s cluster, so k3d is only needed for `k3d`.
    """
    if cluster == "rancher-desktop":
        return tuple(t for t in CHECKS if t != "k3d")
    return CHECKS


def current_cluster() -> str:
    """Cluster flavour from SWARM_CLUSTER (default `k3d`), as the bash side reads it."""
    return os.environ.get("SWARM_CLUSTER", "") or "k3d"


@dataclass(frozen=True)
class CheckResult:
    """Result of a single tool check."""

    name: str
    found: bool
    detail: str

    @property
    def ok(self) -> bool:
        return self.found


def _version_of(binary: str) -> str | None:
    exe = shutil.which(binary)
    if exe is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603
            [exe, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (out.stdout or out.stderr).strip().splitlines()
    return first[0] if first else "installed"


def check_all(tools: tuple[str, ...] | None = None) -> tuple[CheckResult, ...]:
    """Check all tools (default: the current cluster flavour's); keep `tools` order."""
    if tools is None:
        tools = required_tools(current_cluster())
    results: list[CheckResult] = []
    for name in tools:
        version = _version_of(name)
        found = version is not None
        results.append(CheckResult(name=name, found=found, detail=version or "NOT FOUND"))
    return tuple(results)
