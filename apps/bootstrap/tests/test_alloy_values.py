"""Alloy (pod logs → Loki) values invariants."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
VALUES = ROOT / "infra" / "loki" / "alloy.yaml"


def _values() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    return data


def test_alloy_mounts_var_log() -> None:
    assert _values()["alloy"]["mounts"]["varlog"] is True


def test_alloy_mounts_docker_container_logs() -> None:
    # On the moby runtime (Rancher Desktop) /var/log/pods/*/N.log are symlinks
    # into /var/lib/docker/containers; without that mount every tailed file is
    # a dangling link and no pod log reaches Loki (m2b regression, 2026-11-15).
    assert _values()["alloy"]["mounts"]["dockercontainers"] is True
