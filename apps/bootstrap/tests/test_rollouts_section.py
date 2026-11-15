"""Stand wiring for M6: Argo Rollouts, the isv-product Application, demo section."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = (ROOT / "infra" / "scripts" / "bootstrap.sh").read_text(encoding="utf-8")
GITEA_IN_CLUSTER = "http://gitea-http.platform.svc.cluster.local:3000"


def _section(name: str) -> str:
    body = BOOTSTRAP[BOOTSTRAP.index(f"{name}() {{") :]
    return body[: body.index("\n}\n")]


def test_rollouts_section_installs_pinned_chart() -> None:
    body = _section("section_rollouts")
    assert "helm upgrade --install argo-rollouts argo/argo-rollouts" in body
    assert re.search(r"--version \d+\.\d+\.\d+", body)
    assert "rollouts) section_rollouts" in BOOTSTRAP
    all_line = next(line for line in BOOTSTRAP.splitlines() if re.match(r"\s*all\)", line))
    assert all_line.index("section_argocd") < all_line.index("section_rollouts")
    assert all_line.index("section_rollouts") < all_line.index("section_demo")


def test_appproject_allows_gitea_sources() -> None:
    proj: dict[str, Any] = yaml.safe_load(
        (ROOT / "infra" / "argocd" / "appproject.yaml").read_text(encoding="utf-8")
    )
    assert f"{GITEA_IN_CLUSTER}/swarm-lab/*" in proj["spec"]["sourceRepos"]


def test_isv_product_application_tracks_gitea_deploy_dir() -> None:
    app: dict[str, Any] = yaml.safe_load(
        (ROOT / "infra" / "argocd" / "application-isv-product.yaml").read_text(encoding="utf-8")
    )
    assert app["metadata"]["name"] == "isv-product-prod"
    src = app["spec"]["source"]
    assert src["repoURL"] == f"{GITEA_IN_CLUSTER}/swarm-lab/isv-product.git"
    assert src["path"] == "deploy"
    assert app["spec"]["destination"]["namespace"] == "prod"
    assert app["spec"]["syncPolicy"]["automated"]["selfHeal"] is True


def test_demo_section_seeds_builds_and_waits_for_healthy_rollout() -> None:
    body = _section("section_demo")
    for needle in (
        "seed-demo",
        "isv-product:seed",
        "application-isv-product.yaml",
        "argocd.argoproj.io/secret-type=repository",
        "secret/gitea/agent",
        "Healthy",
    ):
        assert needle in body, needle
    assert not (ROOT / "infra" / "argocd" / "application-demo.yaml").exists()
