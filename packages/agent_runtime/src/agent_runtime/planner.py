"""Swarm planner — the coordinator role (M8, spec §7.2 item 3).

An LLM splits a task into a vertex graph ``{name, deps, paths_allow}``; the
platform then VALIDATES it before anything runs:

- names unique and ≤120 chars, deps known, graph acyclic, 1..max vertices;
- every path inside the task's allowed area (the planner cannot widen scope);
- vertices that can run CONCURRENTLY (neither depends on the other,
  transitively) have disjoint paths — one writer per path area.

The planner never supplies commands: ``done_when`` is platform-supplied. Any
failure (HTTP, unparseable, invalid plan) falls back to a single vertex over
the whole allowed area — the swarm degrades to one agent instead of failing.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

MAX_NAME = 120
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_WILDCARDS = "*?["

_SYSTEM = (
    "You are the planning coordinator of a software delivery platform. Split the task "
    "into at most {max} vertices that separate agents implement IN PARALLEL where "
    "possible. Each vertex: a short imperative name (<=120 chars, it is the agent's "
    "instruction), deps (names of vertices that must be merged first) and paths_allow "
    "(exact files or globs it may change). Vertices without a dependency path between "
    "them MUST NOT share files. All paths must stay inside the allowed area. Prefer "
    "independent new files over editing one shared file. Answer ONLY with JSON: "
    '{{"vertices": [{{"name": "...", "deps": [], "paths_allow": ["..."]}}]}}'
)


class PlanError(ValueError):
    """The plan violates the swarm invariants."""


class VertexPlan(BaseModel):
    name: str
    deps: list[str] = Field(default_factory=list)
    paths_allow: list[str]


def _prefix(glob: str) -> str | None:
    """Literal prefix of a glob; None for a literal path."""
    positions = [glob.index(c) for c in _WILDCARDS if c in glob]
    return glob[: min(positions)] if positions else None


def globs_overlap(a: str, b: str) -> bool:
    """Conservative: could ``a`` and ``b`` select a common file?"""
    if a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    pa, pb = _prefix(a), _prefix(b)
    if pa is not None and pb is not None:
        return pa.startswith(pb) or pb.startswith(pa)
    return False


def _inside(path: str, allowed: list[str]) -> bool:
    probe = path if _prefix(path) is None else f"{_prefix(path)}x"
    return any(fnmatch.fnmatch(probe, g) for g in allowed)


def _ancestors(plans: dict[str, VertexPlan]) -> dict[str, set[str]]:
    resolved: dict[str, set[str]] = {}
    visiting: set[str] = set()

    def visit(name: str) -> set[str]:
        if name in resolved:
            return resolved[name]
        if name in visiting:
            raise PlanError(f"dependency cycle through {name!r}")
        visiting.add(name)
        found: set[str] = set()
        for dep in plans[name].deps:
            found |= {dep} | visit(dep)
        visiting.discard(name)
        resolved[name] = found
        return found

    for name in plans:
        visit(name)
    return resolved


def validate_plan(
    plans: list[VertexPlan], allowed: list[str], *, max_vertices: int = 5
) -> list[VertexPlan]:
    if not 1 <= len(plans) <= max_vertices:
        raise PlanError(f"plan must have 1..{max_vertices} vertices, got {len(plans)}")
    by_name: dict[str, VertexPlan] = {}
    for plan in plans:
        if not plan.name.strip() or len(plan.name) > MAX_NAME:
            raise PlanError(f"bad vertex name {plan.name!r}")
        if plan.name in by_name:
            raise PlanError(f"duplicate vertex name {plan.name!r}")
        if not plan.paths_allow:
            raise PlanError(f"vertex {plan.name!r} has no paths")
        for path in plan.paths_allow:
            if not _inside(path, allowed):
                raise PlanError(f"path {path!r} of {plan.name!r} is outside {allowed}")
        by_name[plan.name] = plan
    for plan in plans:
        for dep in plan.deps:
            if dep not in by_name:
                raise PlanError(f"vertex {plan.name!r} has unknown dep {dep!r}")
    ancestors = _ancestors(by_name)
    names = list(by_name)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            if a in ancestors[b] or b in ancestors[a]:
                continue
            for pa in by_name[a].paths_allow:
                for pb in by_name[b].paths_allow:
                    if globs_overlap(pa, pb):
                        raise PlanError(
                            f"concurrent vertices {a!r} and {b!r} share {pa!r} / {pb!r}"
                        )
    return plans


def fallback_plan(title: str, allowed: list[str]) -> list[VertexPlan]:
    """One vertex over the whole allowed area (no parallelism)."""
    name = " ".join(title.split())[:MAX_NAME] or "implement the task"
    return [VertexPlan(name=name, deps=[], paths_allow=list(allowed))]


def _parse(text: str) -> list[VertexPlan]:
    for candidate in [m.group(1) for m in _FENCE_RE.finditer(text)] + [text.strip()]:
        try:
            data: Any = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("vertices"), list):
            return [VertexPlan.model_validate(v) for v in data["vertices"]]
    raise PlanError("no JSON plan in the answer")


async def plan_task(
    http: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model: str,
    title: str,
    body: str,
    allowed: list[str],
    max_vertices: int = 5,
) -> list[VertexPlan]:
    """Planned and validated graph, or the single-vertex fallback."""
    user = f"Task: {title}\n\n{body}\n\nAllowed area: {', '.join(allowed)}"
    try:
        response = await http.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM.format(max=max_vertices)},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 2000,
                "temperature": 0.1,
            },
            timeout=180.0,
        )
        response.raise_for_status()
        content = str(response.json()["choices"][0]["message"]["content"] or "")
        return validate_plan(_parse(content), allowed, max_vertices=max_vertices)
    except (httpx.HTTPError, KeyError, IndexError, ValueError, ValidationError) as exc:
        logger.warning("planner fell back to a single vertex: %s", exc)
        return fallback_plan(title, allowed)
