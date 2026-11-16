"""Per-project intake config `.platform/project.yaml` (M11)."""

from __future__ import annotations

import pytest
from agent_runtime.project_config import parse_project_config

VALID = """
intake: true
paths: ["src/**", "tests/**"]
done_when: "uv run pytest -q"
deploy: {app: isv-product, namespace: prod}
workers_per_run: 2
"""


def test_parse_valid() -> None:
    cfg = parse_project_config(VALID)
    assert cfg.intake is True
    assert cfg.paths == ["src/**", "tests/**"]
    assert cfg.done_when == "uv run pytest -q"
    assert cfg.deploy is not None and cfg.deploy.app == "isv-product"
    assert cfg.deploy.namespace == "prod"
    assert cfg.workers_per_run == 2


def test_defaults() -> None:
    cfg = parse_project_config('intake: false\npaths: ["src/**"]\ndone_when: "true"\n')
    assert cfg.deploy is None
    assert cfg.workers_per_run == 3


@pytest.mark.parametrize(
    "text",
    [
        'intake: true\npaths: ["src/**"]\ndone_when: ""\n',
        'intake: true\npaths: []\ndone_when: "x"\n',
        'intake: true\npaths: ["src/**"]\ndone_when: "x"\nworkers_per_run: 0\n',
        'intake: true\npaths: ["src/**"]\ndone_when: "x"\nworkers_per_run: 4\n',
        'intake: true\npaths: ["src/**"]\ndone_when: "x"\nsurprise: 1\n',
        "- just\n- a list\n",
        "intake: [unclosed\n",
    ],
)
def test_invalid_raises(text: str) -> None:
    with pytest.raises(ValueError):
        parse_project_config(text)


@pytest.mark.parametrize(
    ("profile", "intake"), [("python", True), ("typescript", True), ("platform", False)]
)
def test_check_profiles_ship_a_valid_project_config(profile: str, intake: bool) -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    path = root / "infra/templates/check-profiles" / profile / ".platform/project.yaml"
    assert parse_project_config(path.read_text(encoding="utf-8")).intake is intake


def test_platform_repo_config_is_opt_in() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    text = (root / ".platform/project.yaml").read_text(encoding="utf-8")
    assert parse_project_config(text).intake is False
