"""Project memory client + memory-extractor role (M7)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from agent_runtime.memory import (
    MemoryClient,
    extract_lessons,
    memory_prompt_section,
    parse_lessons,
    project_of,
)


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://api")


async def test_slice_passes_query_and_returns_episodes() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"episodes": [{"id": 1, "kind": "lesson"}]})

    async with _client(handler) as http:
        got = await MemoryClient(http, "http://api").slice("o/r", ["src/**", "tests/**"], "add x")
    assert got == [{"id": 1, "kind": "lesson"}]
    assert seen == {"project": "o/r", "paths": "src/**,tests/**", "q": "add x", "limit": "5"}


async def test_slice_fails_open() -> None:
    async with _client(lambda r: httpx.Response(503)) as http:
        assert await MemoryClient(http, "http://api").slice("o/r", [], "x") == []

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with _client(boom) as http:
        assert await MemoryClient(http, "http://api").slice("o/r", [], "x") == []


async def test_add_returns_id_or_none() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": 7})

    async with _client(handler) as http:
        mem = MemoryClient(http, "http://api")
        assert await mem.add("o/r", "deploy", "t", "b", ["src/a.py"], run_id=1, vertex_id=2) == 7
    assert bodies[0]["kind"] == "deploy" and bodies[0]["vertex_id"] == 2
    async with _client(lambda r: httpx.Response(500)) as http:
        assert await MemoryClient(http, "http://api").add("o/r", "deploy", "t", "b", []) is None


def test_parse_lessons_bare_fenced_and_prose() -> None:
    bare = '{"lessons": [{"title": "a", "body": "b"}]}'
    assert parse_lessons(bare) == [{"title": "a", "body": "b"}]
    fenced = "Here:\n```json\n" + bare + "\n```\n"
    assert parse_lessons(fenced) == [{"title": "a", "body": "b"}]
    assert parse_lessons("I learned that tests matter.") == []
    many = json.dumps({"lessons": [{"title": str(i), "body": "x"} for i in range(5)]})
    assert len(parse_lessons(many)) == 2
    assert parse_lessons('{"lessons": [{"title": "", "body": "x"}, "junk"]}') == []


async def test_extract_lessons_calls_light_model() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        content = '{"lessons": [{"title": "keep routes thin", "body": "x"}]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        lessons = await extract_lessons(
            http,
            base_url="http://litellm/v1",
            api_key="k",
            model="qwen-coder",
            task="add /api/time",
            changed=["src/a.py"],
            review_summary="clean",
        )
    assert lessons == [{"title": "keep routes thin", "body": "x"}]
    assert seen["model"] == "qwen-coder"
    assert "add /api/time" in seen["messages"][-1]["content"]


async def test_extract_lessons_fails_open() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as h:
        assert (
            await extract_lessons(
                h, base_url="http://l/v1", api_key="k", model="m", task="t", changed=[],
                review_summary="",
            )
            == []
        )  # fmt: skip


def test_prompt_section_trims_bodies() -> None:
    text = memory_prompt_section(
        [{"id": 3, "kind": "postmortem", "title": "rolled back", "body": "x" * 1000}]
    )
    assert text.startswith("Project memory (lessons from earlier tasks — follow them):")
    assert "[postmortem] rolled back:" in text
    assert "x" * 400 in text and "x" * 401 not in text
    assert memory_prompt_section([]) == ""


def test_project_of() -> None:
    assert project_of("swarm-lab/isv-product#2", "") == "swarm-lab/isv-product"
    assert project_of("", "http://g:3000/swarm-lab/isv-product.git") == "swarm-lab/isv-product"
