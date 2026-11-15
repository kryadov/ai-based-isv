"""The isv-product seed: app code + deploy/ + check profile in one tree (M6)."""

from __future__ import annotations

from pathlib import Path

from bootstrap.seed import seed_tree


def test_seed_tree_has_app_deploy_and_profile(tmp_path: Path) -> None:
    paths = seed_tree(tmp_path)
    for expected in (
        "Dockerfile",
        "README.md",
        "pyproject.toml",
        "src/isv_product/main.py",
        "tests/test_service.py",
        "deploy/rollout.yaml",
        "deploy/kustomization.yaml",
        ".gitea/workflows/ci.yml",
        ".platform/merge-policy.yaml",
    ):
        assert expected in paths, expected
        assert (tmp_path / expected).is_file()
    assert not [p for p in paths if "__pycache__" in p or p.endswith(".pyc")]
    assert paths == sorted(paths)


def test_seeded_project_is_standalone(tmp_path: Path) -> None:
    seed_tree(tmp_path)
    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    # the python profile runs `uv sync --all-groups` → pytest + httpx must be in dev
    assert "pytest" in text and '"httpx' in text
    assert "[tool.mypy]" in text
