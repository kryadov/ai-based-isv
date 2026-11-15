"""Swarm planner (coordinator role): task → validated vertex graph (M8)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from agent_runtime.planner import (
    PlanError,
    VertexPlan,
    fallback_plan,
    globs_overlap,
    plan_task,
    validate_plan,
)

ALLOWED = ["src/**", "tests/**", "docs/**"]


def _v(name: str, paths: list[str], deps: list[str] | None = None) -> VertexPlan:
    return VertexPlan(name=name, paths_allow=paths, deps=deps or [])


def _good() -> list[VertexPlan]:
    return [
        _v("slug", ["src/isv_product/textutil/slug.py", "tests/test_slug.py"]),
        _v("words", ["src/isv_product/textutil/words.py", "tests/test_words.py"]),
        _v("rev", ["src/isv_product/textutil/rev.py", "tests/test_rev.py"]),
        _v("doc", ["docs/**"], ["slug", "words", "rev"]),
    ]


def test_valid_three_plus_one() -> None:
    assert validate_plan(_good(), ALLOWED) == _good()


@pytest.mark.parametrize(
    ("a", "b", "overlap"),
    [
        ("src/**", "src/a.py", True),
        ("src/a.py", "src/a.py", True),
        ("src/x/**", "src/x/y.py", True),
        ("src/a.py", "src/b.py", False),
        ("src/x/**", "src/y/**", False),
        ("docs/**", "src/**", False),
    ],
)
def test_globs_overlap(a: str, b: str, overlap: bool) -> None:
    assert globs_overlap(a, b) is overlap
    assert globs_overlap(b, a) is overlap


def test_unknown_dep_rejected() -> None:
    with pytest.raises(PlanError, match="unknown dep"):
        validate_plan([_v("a", ["src/a.py"], ["ghost"])], ALLOWED)


def test_cycle_rejected() -> None:
    with pytest.raises(PlanError, match="cycle"):
        validate_plan([_v("a", ["src/a.py"], ["b"]), _v("b", ["src/b.py"], ["a"])], ALLOWED)


def test_too_many_and_empty_rejected() -> None:
    with pytest.raises(PlanError):
        validate_plan([_v(f"v{i}", [f"src/{i}.py"]) for i in range(6)], ALLOWED, max_vertices=5)
    with pytest.raises(PlanError):
        validate_plan([], ALLOWED)


def test_path_outside_allowed_rejected() -> None:
    with pytest.raises(PlanError, match="outside"):
        validate_plan([_v("a", ["deploy/rollout.yaml"])], ALLOWED)


def test_concurrent_overlap_rejected_but_dependent_overlap_allowed() -> None:
    with pytest.raises(PlanError, match="concurrent"):
        validate_plan([_v("a", ["src/**"]), _v("b", ["src/b.py"])], ALLOWED)
    chained = [_v("a", ["src/**"]), _v("b", ["src/b.py"], ["a"])]
    assert validate_plan(chained, ALLOWED) == chained


def test_duplicate_names_rejected() -> None:
    with pytest.raises(PlanError, match="duplicate"):
        validate_plan([_v("a", ["src/a.py"]), _v("a", ["src/b.py"])], ALLOWED)


def test_fallback_is_single_vertex_over_allowed() -> None:
    plan = fallback_plan("Add things to the service and document them", ALLOWED)
    assert len(plan) == 1 and plan[0].paths_allow == ALLOWED and plan[0].deps == []
    assert len(plan[0].name) <= 120


def _llm(content: str | None, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if content is None:
            return httpx.Response(status)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _as_json(plans: list[VertexPlan]) -> dict[str, Any]:
    return {"vertices": [p.model_dump() for p in plans]}


async def _plan(client: httpx.AsyncClient) -> list[VertexPlan]:
    async with client as http:
        return await plan_task(
            http, base_url="http://l/v1", api_key="k", model="qwen3",
            title="text utils", body="add three utils and a doc page", allowed=ALLOWED,
        )  # fmt: skip


async def test_plan_task_parses_fenced_json() -> None:
    content = "Plan:\n```json\n" + json.dumps(_as_json(_good())) + "\n```"
    assert [p.name for p in await _plan(_llm(content))] == ["slug", "words", "rev", "doc"]


async def test_plan_task_falls_back_on_invalid_plan_or_error() -> None:
    cyclic = _as_json([_v("a", ["src/a.py"], ["b"]), _v("b", ["src/b.py"], ["a"])])
    for client in (_llm(json.dumps(cyclic)), _llm("no json"), _llm(None, 500)):
        plan = await _plan(client)
        assert len(plan) == 1 and plan[0].paths_allow == ALLOWED
