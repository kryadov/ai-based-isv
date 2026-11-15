"""kube-prometheus-stack values: Prometheus reachable from the host (M6 deployer)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

VALUES = Path(__file__).resolve().parents[3] / "infra" / "monitoring" / "values.yaml"


def test_prometheus_nodeport_for_host_deployer() -> None:
    values: dict[str, Any] = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    service = values["prometheus"]["service"]
    assert service == {"type": "NodePort", "nodePort": 30090}


def test_worker_default_matches_nodeport() -> None:
    from agent_runtime.settings import WorkerSettings

    assert WorkerSettings.model_fields["prometheus_url"].default == "http://localhost:30090"
