"""Control plane in-cluster (M11): manifests, Argo CD app, image build."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
CP = ROOT / "infra" / "platform" / "control-plane"


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def test_kustomization_lists_every_manifest() -> None:
    kust = _load(CP / "kustomization.yaml")
    assert kust["namespace"] == "platform"
    assert sorted(kust["resources"]) == sorted(
        p.name for p in CP.glob("*.yaml") if p.name != "kustomization.yaml"
    )


def test_service_is_nodeport_30400() -> None:
    svc = _load(CP / "service.yaml")
    assert svc["spec"]["type"] == "NodePort"
    assert svc["spec"]["ports"][0]["nodePort"] == 30400


def test_deployment_local_image_and_secret_env_only() -> None:
    dep = _load(CP / "deployment.yaml")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].startswith("swarm-platform:")
    assert container["imagePullPolicy"] == "Never"
    for env in container["env"]:
        assert "value" not in env, f"{env['name']} must come from a Secret"
        assert env["valueFrom"]["secretKeyRef"]["name"] == "agent-api"
    names = {e["name"] for e in container["env"]}
    assert names == {
        "AGENT_API_DATABASE_URL",
        "AGENT_API_UI_USERS",
        "AGENT_API_SESSION_SECRET",
        "AGENT_API_SERVICE_TOKENS",
    }
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["securityContext"]["runAsNonRoot"] is True


def test_argo_app_tracks_the_platform_repo_path() -> None:
    app = _load(ROOT / "infra" / "argocd" / "application-platform-control.yaml")
    src = app["spec"]["source"]
    assert src["repoURL"].endswith("/swarm-lab/ai-based-isv.git")
    assert src["path"] == "infra/platform/control-plane"
    assert app["spec"]["destination"]["namespace"] == "platform"
    assert app["spec"]["project"] == "swarm-platform"


def test_platform_project_is_confined_to_namespace_platform() -> None:
    proj = _load(ROOT / "infra" / "argocd" / "appproject-platform.yaml")
    assert [d["namespace"] for d in proj["spec"]["destinations"]] == ["platform"]
    target = _load(ROOT / "infra" / "argocd" / "appproject.yaml")
    assert "platform" not in [d["namespace"] for d in target["spec"]["destinations"]]


def test_dockerfile_bases_are_pinned() -> None:
    text = (ROOT / "infra" / "platform" / "Dockerfile").read_text(encoding="utf-8")
    for image in re.findall(r"^FROM (\S+)", text, flags=re.M):
        assert re.search(r":\d+\.\d+\.\d+", image), f"unpinned base image {image}"
    ignore = (ROOT / "infra" / "platform" / "Dockerfile.dockerignore").read_text(encoding="utf-8")
    for secret in ("infra/.vault-*", ".env", ".agent-runtime-workspace"):
        assert secret in ignore
