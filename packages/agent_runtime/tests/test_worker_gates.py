"""Per-repo platform detection: the platform repo never auto-merges (§13.7)."""

from __future__ import annotations

from pathlib import Path

from agent_runtime.settings import WorkerSettings
from agent_runtime.worker_gates import is_platform_repo


def _settings(tmp_path: Path, **extra: object) -> WorkerSettings:
    return WorkerSettings(
        api_url="http://test",
        run_id=1,
        agent="w",
        workspace_dir=tmp_path,
        repo_url="http://gitea:3000/swarm-lab/x.git",
        litellm_base_url="http://litellm/v1",
        model="litellm/qwen3",
        **extra,  # type: ignore[arg-type]
    )


def test_platform_repo_detected_by_slug(tmp_path: Path) -> None:
    assert is_platform_repo(_settings(tmp_path), "swarm-lab", "ai-based-isv")


def test_target_repo_is_not_platform(tmp_path: Path) -> None:
    assert not is_platform_repo(_settings(tmp_path), "swarm-lab", "target-scratch")


def test_explicit_flag_still_wins(tmp_path: Path) -> None:
    assert is_platform_repo(_settings(tmp_path, platform_repo=True), "swarm-lab", "target-scratch")


def test_slug_list_is_configurable(tmp_path: Path) -> None:
    s = _settings(tmp_path, platform_repos=["acme/core"])
    assert is_platform_repo(s, "acme", "core")
    assert not is_platform_repo(s, "swarm-lab", "ai-based-isv")
