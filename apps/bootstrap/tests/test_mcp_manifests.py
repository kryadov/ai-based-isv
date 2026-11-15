"""Network invariants of the MCP servers in `agents` (M4c review I2 + I3).

The write-capable task-source holds the Gitea admin token, so no pod may reach
it — only the host worker through its NodePort. Sandboxes reach only the
servers labelled for sandbox access (the read-only task-reader and llm).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

INFRA = Path(__file__).resolve().parents[3] / "infra"
SANDBOX_ACCESS = "agent-runtime.io/sandbox-access"
POD_CIDR = "10.42.0.0/16"


def _docs(*rel: str) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for r in rel:
        docs += [d for d in yaml.safe_load_all((INFRA / r).read_text(encoding="utf-8")) if d]
    return docs


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"{kind}/{name}: {len(found)} found"
    return found[0]


def _pod_labels(deploy: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = deploy["spec"]["template"]["metadata"]["labels"]
    return labels


def _env(deploy: dict[str, Any]) -> dict[str, Any]:
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container.get("env", [])}


def test_task_reader_is_read_only_and_cluster_internal() -> None:
    docs = _docs("mcp/task-reader.yaml")
    deploy = _one(docs, "Deployment", "task-reader")
    assert _env(deploy)["TASK_SOURCE_READ_ONLY"] == "1"
    assert _pod_labels(deploy)[SANDBOX_ACCESS] == "yes"
    service = _one(docs, "Service", "task-reader")
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"


def test_write_task_source_is_not_sandbox_accessible() -> None:
    deploy = _one(_docs("mcp/task-source.yaml"), "Deployment", "task-source")
    assert SANDBOX_ACCESS not in _pod_labels(deploy)
    assert _env(deploy).get("TASK_SOURCE_READ_ONLY") in (None, "", "0")


def test_llm_is_sandbox_accessible() -> None:
    deploy = _one(_docs("mcp/llm.yaml"), "Deployment", "llm")
    assert _pod_labels(deploy)[SANDBOX_ACCESS] == "yes"


def test_sandbox_egress_to_mcp_only_reaches_sandbox_access_pods() -> None:
    policy = _one(_docs("agents/netpol-allow.yaml"), "NetworkPolicy", "allow-agent-egress")
    mcp_rules = [
        rule
        for rule in policy["spec"]["egress"]
        if any(p.get("port") == 8000 for p in rule.get("ports", []))
    ]
    assert mcp_rules, "no egress rule for the MCP port"
    for rule in mcp_rules:
        for peer in rule["to"]:
            assert peer["podSelector"]["matchLabels"] == {SANDBOX_ACCESS: "yes"}


def test_mcp_ingress_policies_have_sources() -> None:
    docs = _docs("agents/netpol-mcp.yaml")
    shared = _one(docs, "NetworkPolicy", "allow-mcp-ingress")
    assert shared["spec"]["podSelector"]["matchLabels"] == {SANDBOX_ACCESS: "yes"}
    for rule in shared["spec"]["ingress"]:
        assert rule["from"] == [{"podSelector": {}}]

    writer = _one(docs, "NetworkPolicy", "allow-task-source-ingress")
    assert writer["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "task-source"}
    for rule in writer["spec"]["ingress"]:
        peers = rule["from"]
        assert peers, "task-source ingress must name its sources"
        for peer in peers:
            assert set(peer) == {"ipBlock"}, "no pod/namespace selectors may reach task-source"
            block = peer["ipBlock"]
            if block["cidr"] == "0.0.0.0/0":
                assert POD_CIDR in block.get("except", [])
            else:
                assert block["cidr"].endswith("/32")


def test_ci_is_sandbox_accessible_and_cluster_internal() -> None:
    docs = _docs("mcp/ci.yaml")
    deploy = _one(docs, "Deployment", "ci")
    assert _pod_labels(deploy).get("agent-runtime.io/sandbox-access") == "yes"
    assert deploy["spec"]["template"]["spec"]["containers"][0]["command"][-1] == (
        "mcp_servers.ci_server"
    )
    assert _one(docs, "Service", "ci")["spec"]["type"] == "ClusterIP"


def test_section_mcp_applies_ci() -> None:
    text = (Path(__file__).resolve().parents[3] / "infra/scripts/bootstrap.sh").read_text(
        encoding="utf-8"
    )
    assert '"$ROOT/mcp/ci.yaml"' in text
    assert "deploy/ci " in text or "deploy/ci\n" in text


# --- observability (M6): read-only deploy/metrics view, Prometheus egress only ---


def test_observability_server_is_sandbox_accessible_and_secretless() -> None:
    docs = _docs("mcp/observability.yaml")
    deploy = _one(docs, "Deployment", "observability")
    assert _pod_labels(deploy)[SANDBOX_ACCESS] == "yes"
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["python", "-m", "mcp_servers.observability_server"]
    env = _env(deploy)
    assert (
        env["PROMETHEUS_URL"]
        == "http://kube-prometheus-stack-prometheus.platform.svc.cluster.local:9090"
    )
    assert not any("secretKeyRef" in str(e) for e in container.get("env", []))
    assert deploy["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    assert _one(docs, "Service", "observability")["spec"]["ports"][0]["port"] == 8000


def test_only_observability_pods_reach_prometheus() -> None:
    policy = _one(
        _docs("mcp/observability.yaml"), "NetworkPolicy", "allow-observability-prometheus"
    )
    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "observability"
    }
    rule = policy["spec"]["egress"][0]
    assert rule["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "platform"
    }
    assert rule["ports"] == [{"protocol": "TCP", "port": 9090}]
    shared = _one(_docs("agents/netpol-allow.yaml"), "NetworkPolicy", "allow-agent-egress")
    ports = {p["port"] for r in shared["spec"]["egress"] for p in r.get("ports", [])}
    assert 9090 not in ports, "sandboxes must not reach Prometheus directly"


def test_mcp_section_applies_observability_and_rollouts_export_metrics() -> None:
    text = (INFRA / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
    assert 'kubectl apply -f "$ROOT/mcp/observability.yaml"' in text
    assert "deploy/observability" in text
    rollouts = text[text.index("section_rollouts() {") :]
    rollouts = rollouts[: rollouts.index("\n}\n")]
    assert "controller.metrics.serviceMonitor.enabled=true" in rollouts


def test_worker_registers_observability_mcp() -> None:
    from agent_runtime.settings import WorkerSettings
    from agent_runtime.worker import _mcp_servers

    settings = WorkerSettings.model_construct(
        mcp_task_source_url="", mcp_llm_url="", mcp_observability_url="http://o:8000/mcp"
    )
    assert _mcp_servers(settings) == {"observability": "http://o:8000/mcp"}
