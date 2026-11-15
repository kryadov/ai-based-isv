"""Merge policy and protected gate paths (M5, spec §7.2 item 4, §13.7).

A PR merges without a human only when the repo's policy enables auto-merge and
EVERY changed path matches its low-risk globs. A missing or invalid policy
disables auto-merge (fail closed); the platform's own repo never auto-merges.
``.gitea/**`` (the CI corridor) and ``.platform/**`` (this policy) are gate
paths an implementer agent may never change.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import yaml
from pydantic import BaseModel, StrictBool, ValidationError

from agent_runtime.pathguard import filter_violations

PROTECTED_GLOBS: Final[tuple[str, ...]] = (".gitea/**", ".platform/**")


class MergePolicy(BaseModel):
    auto_merge_enabled: bool
    auto_merge_paths: list[str]


class _AutoMerge(BaseModel):
    enabled: StrictBool
    paths: list[str]


_DISABLED = MergePolicy(auto_merge_enabled=False, auto_merge_paths=[])


def touches_protected(paths: Sequence[str]) -> list[str]:
    """The ``paths`` that fall under a protected gate glob, in input order."""
    outside = set(filter_violations(list(paths), list(PROTECTED_GLOBS)))
    return [p for p in paths if p not in outside]


def load_policy(text: str | None, *, platform_repo: bool) -> MergePolicy:
    """Parse ``.platform/merge-policy.yaml``; anything unexpected disables auto-merge."""
    if platform_repo or not text:
        return _DISABLED
    try:
        data: Any = yaml.safe_load(text)
        section = _AutoMerge.model_validate(data["auto_merge"])
    except (yaml.YAMLError, TypeError, KeyError, ValidationError):
        return _DISABLED
    return MergePolicy(auto_merge_enabled=section.enabled, auto_merge_paths=section.paths)


def auto_merge_allowed(paths: Sequence[str], policy: MergePolicy) -> bool:
    """True only for a non-empty change set entirely inside the policy's globs."""
    if not policy.auto_merge_enabled or not paths or not policy.auto_merge_paths:
        return False
    return not filter_violations(list(paths), policy.auto_merge_paths)
