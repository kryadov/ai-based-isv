"""act_runner manifest: the runner reaches Gitea on the SERVICE port (M5 live fix)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_runner_uses_the_gitea_service_port_not_the_nodeport() -> None:
    docs: list[dict[str, Any]] = [
        d
        for d in yaml.safe_load_all((ROOT / "infra/gitea/act-runner.yaml").read_text("utf-8"))
        if d
    ]
    deploy = next(d for d in docs if d["kind"] == "Deployment")
    env = {
        e["name"]: e.get("value")
        for c in deploy["spec"]["template"]["spec"]["containers"]
        for e in c.get("env", [])
    }
    # gitea-http is NodePort 3000:30080 — inside the cluster the port is 3000.
    assert env["GITEA_INSTANCE_URL"] == "http://gitea-http.platform.svc.cluster.local:3000"


def test_runner_serves_the_profile_runs_on_label() -> None:
    # The check profile's jobs say `runs-on: ubuntu-latest`; a runner without
    # that label leaves every CI job waiting forever (m5 live run 1).
    docs = list(yaml.safe_load_all((ROOT / "infra/gitea/act-runner.yaml").read_text("utf-8")))
    config = yaml.safe_load(
        next(d for d in docs if d and d["kind"] == "ConfigMap")["data"]["config.yaml"]
    )
    names = {label.split(":", 1)[0] for label in config["runner"]["labels"]}
    assert "ubuntu-latest" in names


def test_section_runner_restarts_to_pick_up_config_changes() -> None:
    # The labels live in a ConfigMap: without a restart a changed config is
    # silently ignored by the running daemon (m5 live run).
    text = (ROOT / "infra/scripts/bootstrap.sh").read_text("utf-8")
    assert "rollout restart deploy/act-runner" in text
