"""git MCP server (worker-side, audited) over a fake GitOpsLike (M4c Task 3)."""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp_servers.git_server import create_git_server


class FakeGitOps:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail = fail

    def clone_status(self) -> dict[str, str]:
        self.calls.append(("clone_status",))
        return {"repo_url": "http://gitea/o/r", "base_branch": "main", "head": "a" * 40}

    def apply_and_push(self, diff: str, work_branch: str, message: str) -> str:
        self.calls.append(("apply_and_push", diff, work_branch, message))
        if self.fail is not None:
            raise self.fail
        return "b" * 40


async def test_tools_are_exposed() -> None:
    async with Client(create_git_server(FakeGitOps())) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"clone_status", "apply_and_push"}


async def test_clone_status_passes_through() -> None:
    gitops = FakeGitOps()
    async with Client(create_git_server(gitops)) as client:
        result = await client.call_tool("clone_status", {})
    assert result.structured_content is not None
    assert result.structured_content["base_branch"] == "main"
    assert gitops.calls == [("clone_status",)]


async def test_apply_and_push_records_call_and_returns_audited_result() -> None:
    gitops = FakeGitOps()
    args: dict[str, Any] = {
        "run_id": 3,
        "vertex_id": 9,
        "diff": "diff --git a/x b/x\n",
        "work_branch": "agent/vertex-9",
        "message": "agent: thing",
    }
    async with Client(create_git_server(gitops)) as client:
        result = await client.call_tool("apply_and_push", args)
    payload = result.structured_content
    assert payload is not None
    assert payload["sha"] == "b" * 40
    assert payload["branch"] == "agent/vertex-9"
    assert payload["run_id"] == 3
    assert payload["vertex_id"] == 9
    assert "audit" in payload
    assert gitops.calls == [
        ("apply_and_push", "diff --git a/x b/x\n", "agent/vertex-9", "agent: thing")
    ]


async def test_worker_adapter_satisfies_gitopslike(tmp_path: Any) -> None:
    # Contract between the packages: the worker's adapter plugs into the server
    # (mypy checks the structural match; the call proves the runtime wiring).
    from agent_runtime.git_adapter import WorkerGitOps
    from mcp_servers.git_server import GitOpsLike

    ops: GitOpsLike = WorkerGitOps(tmp_path, repo_url="http://g/o/r", base_branch="main")
    async with Client(create_git_server(ops)) as client:
        with pytest.raises(ToolError):  # tmp_path is not a git repo
            await client.call_tool("clone_status", {})


async def test_gitops_failure_is_a_tool_error() -> None:
    gitops = FakeGitOps(fail=RuntimeError("refusing to apply an empty diff"))
    async with Client(create_git_server(gitops)) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(
                "apply_and_push",
                {"run_id": 1, "vertex_id": 1, "diff": "", "work_branch": "b", "message": "m"},
            )
    assert "empty diff" in str(excinfo.value)
