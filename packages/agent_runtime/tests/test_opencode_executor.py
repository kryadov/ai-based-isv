"""Tests for the OpenCode executor adapter and docker serve launcher."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from agent_runtime.executor import ExecutionRequest, ExecutionStatus
from agent_runtime.opencode_executor import DockerServe, OpencodeExecutor
from agent_runtime.settings import OPENCODE_IMAGE

_OPENCODE_PORT = 4096


def _request() -> ExecutionRequest:
    return ExecutionRequest(
        run_id=7,
        vertex_id=70,
        name="add-greeting",
        prompt="Add a greeting function and a test for it.",
        paths_allow=["src/**", "tests/**"],
        done_when='{"checks": [{"type": "cmd", "run": "pytest -q"}]}',
        repo_url="http://gitea-http.swarm-lab.svc:3000/swarm-lab/target-scratch.git",
        work_branch="agent/vertex-70",
    )


def _evidence_dir(workspace: Path, request: ExecutionRequest) -> Path:
    return (
        workspace
        / ".agent_runtime"
        / "runs"
        / f"run-{request.run_id}"
        / f"vertex-{request.vertex_id}"
    )


def _handler(
    *,
    events: list[dict[str, Any]],
    diff_items: list[dict[str, Any]],
    status_tokens: dict[str, Any] | None = None,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/api/session" and method == "POST":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "ses_test",
                        "projectID": "proj_test",
                        "model": {"id": "qwen-coder", "providerID": "litellm"},
                        "location": {"directory": "/workspace"},
                    }
                },
            )

        if path == "/api/session/ses_test/prompt" and method == "POST":
            return httpx.Response(200, json={"data": {"id": "msg_test"}})

        if path == "/api/event" and method == "GET":
            body = ""
            for event in events:
                body += f"data: {json.dumps(event)}\n\n"
            return httpx.Response(
                200,
                content=body.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )

        if path == "/api/session/ses_test/diff" and method == "GET":
            return httpx.Response(200, json={"data": diff_items})

        if path == "/api/session/ses_test" and method == "GET":
            data: dict[str, Any] = {"id": "ses_test", "outcome": "succeeded"}
            if status_tokens is not None:
                data["tokens"] = status_tokens
            return httpx.Response(200, json={"data": data})

        return httpx.Response(404)

    return handler


def test_docker_serve_builds_expected_argv(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> None:
        commands.append(argv)

    serve = DockerServe(
        image=OPENCODE_IMAGE,
        repo_dir=repo_dir,
        port=12345,
        env={"LITELLM_API_KEY": "secret"},
        runner=runner,
    )
    base_url = serve.start()

    assert base_url == "http://127.0.0.1:12345"

    build = next((c for c in commands if c[0] == "docker" and "build" in c), None)
    run = next((c for c in commands if c[0] == "docker" and "run" in c), None)

    assert build is not None
    assert "opencode-git:2.2.0" in build

    assert run is not None
    assert f"-v{repo_dir}:/workspace" in run or f"-v{repo_dir}:/workspace" in run
    assert "-p12345:4096" in run or "-p" in run and "12345:4096" in run
    assert "serve" in run
    assert "--hostname" in run
    assert "0.0.0.0" in run
    assert "--port" in run
    assert "4096" in run
    assert "opencode-git:2.2.0" in run


async def test_start_returns_session_and_streams_events(tmp_path: Path) -> None:
    events = [
        {
            "id": "evt_1",
            "type": "session.text.delta",
            "data": {"sessionID": "ses_test", "delta": "hello"},
        },
        {
            "id": "evt_2",
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_test"},
        },
    ]
    transport = httpx.MockTransport(
        _handler(events=events, diff_items=[], status_tokens=None)
    )
    executor = OpencodeExecutor(
        base_url="http://localhost:8080",
        workspace=tmp_path,
        model="qwen-coder",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        transport=transport,
    )
    request = _request()
    session = await executor.start(request)

    seen = [event async for event in session.events()]
    assert [event.type for event in seen] == [
        "session.text.delta",
        "session.execution.succeeded",
    ]

    evidence = _evidence_dir(tmp_path, request)
    manifest_path = evidence / "manifest.json"
    started_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert started_manifest["ts_end"] is None
    assert started_manifest["diff_stat"] is None
    assert started_manifest["tokens_used"] is None

    status = await session.wait_done()
    assert status == ExecutionStatus.DONE

    log_path = evidence / "session.ndjson"
    log = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert log["type"] == "prompt"
    records = map(json.loads, log_path.read_text(encoding="utf-8").splitlines())
    harness = [r for r in records if r["type"] == "harness_event"]
    assert len(harness) == 2

    manifest_path = evidence / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["ts_end"] is not None
    assert manifest["ts_start"] is not None


async def test_diff_and_result_after_done(tmp_path: Path) -> None:
    patch = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    events = [
        {
            "id": "evt_1",
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_test"},
        }
    ]
    diff_items = [
        {
            "file": "README.md",
            "patch": patch,
            "additions": 1,
            "deletions": 1,
            "status": "modified",
        }
    ]
    transport = httpx.MockTransport(
        _handler(
            events=events,
            diff_items=diff_items,
            status_tokens={
                "input": 5,
                "output": 7,
                "reasoning": 1,
                "cache": {"read": 0, "write": 0},
            },
        )
    )
    executor = OpencodeExecutor(
        base_url="http://localhost:8080",
        workspace=tmp_path,
        model="qwen-coder",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        transport=transport,
    )
    request = _request()
    session = await executor.start(request)

    assert await session.wait_done() == ExecutionStatus.DONE
    result = session.result
    assert result is not None
    assert "diff --git a/README.md b/README.md" in result.diff
    assert result.diff_files == ["README.md"]
    assert result.session_log_path == _evidence_dir(tmp_path, request) / "session.ndjson"
    assert result.manifest_path == _evidence_dir(tmp_path, request) / "manifest.json"
    assert result.tokens_used == 13

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["diff_stat"] == "1 file changed, 1 insertion(+), 1 deletion(-)"
    assert manifest["tokens_used"] == 13


async def test_stream_cut_is_not_done(tmp_path: Path) -> None:
    events = [
        {
            "id": "evt_1",
            "type": "session.text.delta",
            "data": {"sessionID": "ses_test", "delta": "hello"},
        }
    ]
    transport = httpx.MockTransport(
        _handler(events=events, diff_items=[], status_tokens=None)
    )
    executor = OpencodeExecutor(
        base_url="http://localhost:8080",
        workspace=tmp_path,
        model="qwen-coder",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        transport=transport,
    )
    request = _request()
    session = await executor.start(request)

    status = await session.wait_done()
    assert status == ExecutionStatus.FAILED
    assert session.result is None


class _HangingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False
        self._done = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        event = json.dumps(
            {
                "id": "evt_1",
                "type": "session.created",
                "data": {"sessionID": "ses_test"},
            }
        )
        yield f"data: {event}\n\n".encode()
        await self._done.wait()

    async def aclose(self) -> None:
        self.closed = True
        self._done.set()


async def test_stop_stops(tmp_path: Path) -> None:
    stream = _HangingStream()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/api/session" and method == "POST":
            return httpx.Response(
                200, json={"data": {"id": "ses_test", "location": {"directory": "/workspace"}}}
            )
        if path == "/api/session/ses_test/prompt" and method == "POST":
            return httpx.Response(200, json={"data": {"id": "msg_test"}})
        if path == "/api/event" and method == "GET":
            return httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "text/event-stream"},
            )
        if path == "/api/session/ses_test" and method == "GET":
            return httpx.Response(200, json={"data": {"id": "ses_test", "outcome": "succeeded"}})
        return httpx.Response(404)

    executor = OpencodeExecutor(
        base_url="http://localhost:8080",
        workspace=tmp_path,
        model="qwen-coder",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        transport=httpx.MockTransport(handler),
    )
    session = await executor.start(_request())
    await session.stop()

    assert await session.status() == ExecutionStatus.STOPPED
    assert await session.wait_done() == ExecutionStatus.STOPPED
    assert stream.closed
