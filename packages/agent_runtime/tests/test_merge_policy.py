"""Merge policy and protected gate paths (M5 Task 5)."""

from __future__ import annotations

from agent_runtime.merge_policy import (
    MergePolicy,
    auto_merge_allowed,
    load_policy,
    touches_protected,
)

POLICY = 'auto_merge:\n  enabled: true\n  paths: ["docs/**", "*.md"]\n'


def test_touches_protected() -> None:
    paths = [".gitea/workflows/ci.yml", ".platform/merge-policy.yaml", "src/x.py"]
    assert touches_protected(paths) == [".gitea/workflows/ci.yml", ".platform/merge-policy.yaml"]
    assert touches_protected(["src/x.py", "docs/a.md"]) == []


def test_docs_only_auto_merge() -> None:
    policy = load_policy(POLICY, platform_repo=False)
    assert auto_merge_allowed(["docs/guide/a.md", "README.md"], policy) is True


def test_mixed_paths_need_hitl() -> None:
    policy = load_policy(POLICY, platform_repo=False)
    assert auto_merge_allowed(["docs/a.md", "src/x.py"], policy) is False


def test_platform_repo_never_auto_merges() -> None:
    policy = load_policy(POLICY, platform_repo=True)
    assert policy == MergePolicy(auto_merge_enabled=False, auto_merge_paths=[])
    assert auto_merge_allowed(["docs/a.md"], policy) is False


def test_missing_or_invalid_policy_disables_auto_merge() -> None:
    for text in (None, "", "::: not yaml", "auto_merge: 3", "auto_merge:\n  enabled: yes-please\n"):
        policy = load_policy(text, platform_repo=False)
        assert auto_merge_allowed(["docs/a.md"], policy) is False, text


def test_disabled_policy_never_auto_merges() -> None:
    policy = load_policy(
        'auto_merge:\n  enabled: false\n  paths: ["docs/**"]\n', platform_repo=False
    )
    assert auto_merge_allowed(["docs/a.md"], policy) is False


def test_empty_paths_never_auto_merge() -> None:
    assert auto_merge_allowed([], load_policy(POLICY, platform_repo=False)) is False
