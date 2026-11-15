"""Per-stack check profiles seeded into target repos (M5 Task 2)."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from bootstrap.profiles import CI_CONTEXT, profile_files


def _workflow() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(profile_files("python")[".gitea/workflows/ci.yml"])
    return data


def _run_text() -> str:
    jobs = _workflow()["jobs"]
    return "\n".join(
        str(step.get("run", "")) for job in jobs.values() for step in job.get("steps", [])
    )


def test_python_profile_has_workflow_and_policy() -> None:
    assert set(profile_files("python")) == {
        ".gitea/workflows/ci.yml",
        ".platform/merge-policy.yaml",
    }


def test_workflow_runs_all_gates() -> None:
    text = _run_text()
    for gate in ("ruff check", "ruff format --check", "mypy", "pytest", "gitleaks", "semgrep"):
        assert gate in text, gate


def test_workflow_has_no_skip_switches() -> None:
    raw = profile_files("python")[".gitea/workflows/ci.yml"]
    assert "continue-on-error" not in raw
    assert "|| true" not in raw


def test_ci_context_names_the_checks_job() -> None:
    assert "checks" in _workflow()["jobs"]
    assert CI_CONTEXT.startswith("ci / checks")


def test_merge_policy_defaults_to_docs_only() -> None:
    policy = yaml.safe_load(profile_files("python")[".platform/merge-policy.yaml"])
    assert policy == {"auto_merge": {"enabled": True, "paths": ["docs/**", "*.md"]}}


def test_unknown_stack_raises() -> None:
    with pytest.raises(ValueError):
        profile_files("cobol")
