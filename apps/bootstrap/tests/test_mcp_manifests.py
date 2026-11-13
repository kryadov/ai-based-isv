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
