"""task-source FastMCP server over a fake Gitea (M4c Task 2)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp_servers.task_source import create_task_source_app, parse_task_ref

TOKEN = "gitea-admin-SECRET-0123"
BASE = "http://gitea.test"


class FakeGitea:
    """In-memory Gitea issues API: records every request."""

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {
            5: {"number": 5, "title": "Add reverse", "state": "open", "body": "spec"},
            6: {"number": 6, "title": "Other", "state": "open", "body": ""},
        }
        self.comments: list[tuple[int, str]] = []
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, text=f"upstream boom token {TOKEN}")
        parts = request.url.path.split("/")  # /api/v1/repos/o/r/issues[/n[/comments]]
        if parts[4:6] != ["o", "r"]:
            return httpx.Response(404, text="no repo")
        if len(parts) == 7 and request.method == "GET":
            return httpx.Response(200, json=[self._full(n) for n in sorted(self.issues)])
        number = int(parts[7])
        if number not in self.issues:
            return httpx.Response(404, text="issue not found")
        if len(parts) == 8 and request.method == "GET":
            return httpx.Response(200, json=self._full(number))
        if len(parts) == 8 and request.method == "PATCH":
            self.issues[number]["state"] = json.loads(request.content)["state"]
            return httpx.Response(201, json=self._full(number))
        if len(parts) == 9 and request.method == "POST":
            self.comments.append((number, json.loads(request.content)["body"]))
            return httpx.Response(201, json={"id": len(self.comments)})
        return httpx.Response(405)

    def _full(self, number: int) -> dict[str, Any]:
        return {**self.issues[number], "html_url": f"{BASE}/o/r/issues/{number}"}


@pytest.fixture
def gitea() -> FakeGitea:
    return FakeGitea()


@pytest.fixture
def app(gitea: FakeGitea) -> Any:
    http = httpx.AsyncClient(transport=httpx.MockTransport(gitea.handler))
    return create_task_source_app(base_url=BASE, token=TOKEN, http=http)


def test_parse_task_ref() -> None:
    assert parse_task_ref("swarm-lab/target-scratch#12") == ("swarm-lab", "target-scratch", 12)
    for bad in ("", "o/r", "o#1", "o/r#x", "o/r#0", "/r#1", "o/r/x#1"):
        with pytest.raises(ValueError):
            parse_task_ref(bad)


async def test_tools_are_exposed(app: Any) -> None:
    async with Client(app) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"get_task", "list_tasks", "set_task_status", "add_task_comment"}


async def test_get_task_returns_issue_detail(app: Any) -> None:
    async with Client(app) as client:
        result = await client.call_tool("get_task", {"task_ref": "o/r#5"})
    task = result.structured_content
    assert task is not None
    assert task["task_ref"] == "o/r#5"
    assert task["title"] == "Add reverse"
    assert task["body"] == "spec"
    assert task["state"] == "open"


async def test_list_tasks_returns_refs(app: Any) -> None:
    async with Client(app) as client:
        result = await client.call_tool("list_tasks", {"task_ref_prefix": "o/r"})
    payload = result.structured_content
    assert payload is not None
    assert [t["task_ref"] for t in payload["result"]] == ["o/r#5", "o/r#6"]


@pytest.mark.parametrize(
    ("status", "expected_state"),
    [("done", "closed"), ("in-progress", "open"), ("blocked", "open"), ("open", "open")],
)
async def test_set_task_status_maps_state_and_posts_status_comment(
    app: Any, gitea: FakeGitea, status: str, expected_state: str
) -> None:
    gitea.issues[5]["state"] = "closed" if status != "done" else "open"
    async with Client(app) as client:
        await client.call_tool(
            "set_task_status", {"task_ref": "o/r#5", "status": status, "note": "why"}
        )
    assert gitea.issues[5]["state"] == expected_state
    assert len(gitea.comments) == 1
    number, body = gitea.comments[0]
    assert number == 5
    assert f"status: {status}" in body
    assert "why" in body


async def test_set_task_status_rejects_unknown_status(app: Any, gitea: FakeGitea) -> None:
    async with Client(app) as client:
        with pytest.raises(ToolError):
            await client.call_tool("set_task_status", {"task_ref": "o/r#5", "status": "wontfix"})
    assert gitea.requests == []


async def test_add_task_comment(app: Any, gitea: FakeGitea) -> None:
    async with Client(app) as client:
        await client.call_tool("add_task_comment", {"task_ref": "o/r#6", "body": "hello"})
    assert gitea.comments == [(6, "hello")]


async def test_unknown_task_ref_is_tool_error_not_crash(app: Any) -> None:
    async with Client(app) as client:
        with pytest.raises(ToolError):
            await client.call_tool("get_task", {"task_ref": "not-a-ref"})
        with pytest.raises(ToolError):
            await client.call_tool("get_task", {"task_ref": "o/r#404"})
        # The server is still alive after the errors.
        ok = await client.call_tool("get_task", {"task_ref": "o/r#5"})
    assert ok.structured_content is not None


async def test_upstream_error_never_leaks_the_token(app: Any, gitea: FakeGitea) -> None:
    gitea.fail_with = 500
    async with Client(app) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("get_task", {"task_ref": "o/r#5"})
    assert TOKEN not in str(excinfo.value)


async def test_tool_calls_are_logged_without_token(
    app: Any, gitea: FakeGitea, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mcp_servers.task_source")
    gitea.fail_with = 500
    async with Client(app) as client:
        with pytest.raises(ToolError):
            await client.call_tool("set_task_status", {"task_ref": "o/r#5", "status": "done"})
    text = caplog.text
    assert "set_task_status" in text
    assert "o/r#5" in text
    assert TOKEN not in text
