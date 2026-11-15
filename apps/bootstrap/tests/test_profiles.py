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


def test_checkout_uses_the_platform_git_url() -> None:
    # Gitea's ROOT_URL (localhost NodePort) is unreachable from a job container;
    # the stand sets the org variable PLATFORM_GIT_URL to the in-cluster URL.
    for job in _workflow()["jobs"].values():
        checkout = next(
            s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
        )
        assert "vars.PLATFORM_GIT_URL" in checkout["with"]["github-server-url"]


def test_semgrep_runs_with_a_compatible_pydantic() -> None:
    # semgrep 1.140.0 crashes on import with pydantic>=2.12 (m5 live run 2).
    raw = profile_files("python")[".gitea/workflows/ci.yml"]
    assert '--with "pydantic<2.12"' in raw


def test_actions_are_pinned_to_commit_shas() -> None:
    # semgrep github-actions-mutable-action-tag (the corridor caught it live).
    import re

    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


def test_no_curl_pipe_to_shell_and_uv_checksum_verified() -> None:
    # semgrep gha-curl-pipe-shell: download, verify the checksum, then extract.
    import re

    text = _run_text()
    assert not re.search(r"(curl|wget)[^\n]*\|\s*(sh|bash)\b", text)
    assert "sha256sum -c" in text
