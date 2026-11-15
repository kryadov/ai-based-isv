"""ci MCP server: read-only PR check status and job logs (M5 Task 3)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastmcp import Client
from mcp_servers.ci_server import create_ci_app, parse_pr_ref

TOKEN = "gitea-admin-SECRET-0123"
BASE = "http://gitea.test"


class FakeGitea:
    def __init__(self) -> None:
        self.fail_logs = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/repos/o/r/pulls/3":
            return httpx.Response(200, json={"number": 3, "head": {"sha": "abc"}})
        if path == "/api/v1/repos/o/r/commits/abc/status":
            return httpx.Response(
                200,
                json={
                    "state": "failure",
                    "statuses": [
                        {
                            "context": "ci / checks (pull_request)",
                            "status": "failure",
                            "target_url": f"{BASE}/o/r/actions/runs/9/jobs/41",
                        }
                    ],
                },
            )
        if path == "/api/v1/repos/o/r/actions/jobs/41/logs":
            if self.fail_logs:
                return httpx.Response(500, text=f"boom {TOKEN}")
            return httpx.Response(200, text="\n".join(f"line {i}" for i in range(500)))
        return httpx.Response(404)


@pytest.fixture
def gitea() -> FakeGitea:
    return FakeGitea()


@pytest.fixture
def app(gitea: FakeGitea) -> Any:
    http = httpx.AsyncClient(transport=httpx.MockTransport(gitea.handler))
    return create_ci_app(base_url=BASE, token=TOKEN, http=http)


def test_parse_pr_ref() -> None:
    assert parse_pr_ref("o/r!3") == ("o", "r", 3)
    for bad in ("o/r#3", "o/r!x", "o!3", "", "o/r!0"):
        with pytest.raises(ValueError):
            parse_pr_ref(bad)


async def test_tools_are_read_only(app: Any) -> None:
    async with Client(app) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"get_pr_checks", "get_job_log"}


async def test_get_pr_checks_reports_state(app: Any) -> None:
    async with Client(app) as client:
        result = await client.call_tool("get_pr_checks", {"pr_ref": "o/r!3"})
    data = result.structured_content
    assert data is not None
    assert data["state"] == "failure"
    assert data["statuses"][0]["context"] == "ci / checks (pull_request)"


async def test_get_job_log_returns_tail(app: Any) -> None:
    async with Client(app) as client:
        result = await client.call_tool(
            "get_job_log",
            {"pr_ref": "o/r!3", "context": "ci / checks (pull_request)", "tail_lines": 10},
        )
    text = result.content[0].text
    assert text.splitlines()[-1] == "line 499"
    assert len(text.splitlines()) == 10


async def test_get_job_log_failure_is_explained_without_token(app: Any, gitea: FakeGitea) -> None:
    gitea.fail_logs = True
    async with Client(app) as client:
        result = await client.call_tool(
            "get_job_log", {"pr_ref": "o/r!3", "context": "ci / checks (pull_request)"}
        )
    text = result.content[0].text
    assert "unavailable" in text
    assert TOKEN not in text
