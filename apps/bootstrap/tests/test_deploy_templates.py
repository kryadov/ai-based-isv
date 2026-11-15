"""Deploy templates for service projects: Argo Rollouts canary + analysis (M6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEPLOY = Path(__file__).resolve().parents[3] / "infra" / "templates" / "deploy" / "service"
PROM = "http://kube-prometheus-stack-prometheus.platform:9090"


def _docs(name: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all((DEPLOY / name).read_text(encoding="utf-8")) if d]


def _one(name: str, kind: str) -> dict[str, Any]:
    return next(d for d in _docs(name) if d["kind"] == kind)


def test_rollout_canary_steps() -> None:
    rollout = _one("rollout.yaml", "Rollout")
    canary = rollout["spec"]["strategy"]["canary"]
    steps = canary["steps"]
    assert steps[:5] == [
        {"setWeight": 20},
        {"pause": {"duration": "60s"}},
        {"setWeight": 50},
        {"pause": {"duration": "60s"}},
        {"setWeight": 100},
    ]
    post = steps[5]["analysis"]
    assert post["templates"] == [{"templateName": "post-rollout"}]
    assert {a["name"] for a in post["args"]} == {"baseline-error", "baseline-p95"}
    assert canary["analysis"]["templates"] == [{"templateName": "canary-vs-stable"}]
    hashes = {a["name"]: a["valueFrom"]["podTemplateHashValue"] for a in canary["analysis"]["args"]}
    assert hashes == {"canary-hash": "Latest", "stable-hash": "Stable"}
    assert rollout["spec"]["replicas"] == 4
    container = rollout["spec"]["template"]["spec"]["containers"][0]
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["image"] == "isv-product"
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"


def test_servicemonitor_labels_version() -> None:
    sm = _one("service.yaml", "ServiceMonitor")
    assert "rollouts-pod-template-hash" in sm["spec"]["podTargetLabels"]
    assert sm["spec"]["endpoints"][0]["path"] == "/metrics"


def test_analysis_thresholds() -> None:
    names = set()
    for tpl in _docs("analysis.yaml"):
        assert tpl["kind"] == "AnalysisTemplate"
        names.add(tpl["metadata"]["name"])
        for metric in tpl["spec"]["metrics"]:
            assert metric["interval"] == "30s"
            assert metric["count"] == 4
            assert metric["provider"]["prometheus"]["address"] == PROM
            if metric["name"] == "canary-traffic":
                continue
            assert metric["failureLimit"] == 1
            assert metric["successCondition"] == "result[0] == 1"
    assert names == {"canary-vs-stable", "post-rollout"}


def test_analysis_queries_have_empty_vector_floor() -> None:
    for tpl in _docs("analysis.yaml"):
        for metric in tpl["spec"]["metrics"]:
            if metric["name"] == "canary-traffic":
                continue
            query = metric["provider"]["prometheus"]["query"]
            assert "or vector(0)" in query, metric["name"]
            assert ("0.05" in query) or ("0.3" in query), metric["name"]
            assert "<= bool" in query, metric["name"]


def test_canary_queries_select_by_pod_hash() -> None:
    tpl = next(t for t in _docs("analysis.yaml") if t["metadata"]["name"] == "canary-vs-stable")
    for metric in tpl["spec"]["metrics"]:
        if metric["name"] == "canary-traffic":
            continue
        query = metric["provider"]["prometheus"]["query"]
        assert "{{args.canary-hash}}" in query and "{{args.stable-hash}}" in query


def test_k6_targets_service() -> None:
    cm = _one("k6.yaml", "ConfigMap")
    script = next(iter(cm["data"].values()))
    assert "http://isv-product.prod:8000/api/greet" in script
    deploy = _one("k6.yaml", "Deployment")
    image = deploy["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.startswith("grafana/k6:") and not image.endswith(":latest")


def test_kustomization_lists_all_and_image() -> None:
    kust = yaml.safe_load((DEPLOY / "kustomization.yaml").read_text(encoding="utf-8"))
    assert set(kust["resources"]) == {
        "rollout.yaml",
        "service.yaml",
        "analysis.yaml",
        "k6.yaml",
    }
    assert kust["images"] == [{"name": "isv-product", "newName": "isv-product", "newTag": "seed"}]


def test_canary_without_traffic_fails_analysis() -> None:
    # Live lesson (M6): with keep-alive load the canary got no API traffic and
    # the floors passed vacuously. The canary must have served real requests.
    tpl = next(t for t in _docs("analysis.yaml") if t["metadata"]["name"] == "canary-vs-stable")
    traffic = next(m for m in tpl["spec"]["metrics"] if m["name"] == "canary-traffic")
    assert traffic["successCondition"] == "result[0] > 0"
    assert traffic["failureLimit"] == 2  # the first sample may precede the first scrape
    assert "{{args.canary-hash}}" in traffic["provider"]["prometheus"]["query"]


def test_k6_spreads_load_over_new_pods() -> None:
    script = next(iter(_one("k6.yaml", "ConfigMap")["data"].values()))
    assert "noConnectionReuse: true" in script
