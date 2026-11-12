"""Tests for the OpenCode executor adapter and docker serve launcher."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from agent_runtime.executor import ExecutionRequest, ExecutionStatus
from agent_runtime.opencode_executor import DockerServe, OpencodeExecutor, OpencodeSession
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
) -> Callable[[httpx.Request], httpx.Response]:
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
    commands: list[tuple[list[str], str | None, dict[str, str] | None]] = []

    def runner(
        argv: list[str],
        input: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        commands.append((argv, input, env))
        if argv[0] == "docker" and "run" in argv:
            return subprocess.CompletedProcess(argv, returncode=0, stdout="cid123\n")
        if argv[0] == "docker" and "logs" in argv:
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout="server password fast-token\n"
            )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    serve = DockerServe(
        image=OPENCODE_IMAGE,
        repo_dir=repo_dir,
        port=12345,
        env={"LITELLM_API_KEY": "secret"},
        runner=runner,
    )
    base_url = serve.start()

    assert base_url == "http://127.0.0.1:12345"
    assert serve.auth == ("opencode", "fast-token")

    build = next((c for c in commands if c[0][0] == "docker" and "build" in c[0]), None)
    run = next((c for c in commands if c[0][0] == "docker" and "run" in c[0]), None)

    assert build is not None
    build_argv, build_input, _ = build
    assert "opencode-git:2.2.0" in build_argv
    assert build_input is not None
    assert f"FROM {OPENCODE_IMAGE}" in build_input
    assert "apt-get install -y git" in build_input or "apk add --no-cache git" in build_input
    # Bind mounts owned by another uid (WSL2 drvfs, uid 1000 vs root in the
    # container) make git refuse the repo -> the harness diff comes back empty.
    assert "git config --system --add safe.directory '*'" in build_input

    assert run is not None
    run_argv, _, run_env = run
    assert f"-v{repo_dir}:/workspace" in run_argv
    assert "-p127.0.0.1:12345:4096" in run_argv
    assert run_argv[run_argv.index("-e") + 1] == "LITELLM_API_KEY"
    assert all("secret" not in arg for arg in run_argv)
    assert run_env is not None
    assert run_env["LITELLM_API_KEY"] == "secret"
    assert "serve" in run_argv
    assert "--hostname" in run_argv
    assert "0.0.0.0" in run_argv
    assert "--port" in run_argv
    assert "4096" in run_argv
    assert "opencode-git:2.2.0" in run_argv


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
    transport = httpx.MockTransport(_handler(events=events, diff_items=[], status_tokens=None))
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

    log_path = evidence / "session_log.ndjson"
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
    assert result.session_log_path == _evidence_dir(tmp_path, request) / "session_log.ndjson"
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
    transport = httpx.MockTransport(_handler(events=events, diff_items=[], status_tokens=None))
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


def test_scrape_password_extracts_token_from_noise() -> None:
    logs = "bootstrap line\nserver password abc-xyz-123\nanother line\n"

    def runner(
        argv: list[str],
        input: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode=0, stdout=logs)

    serve = DockerServe(
        image=OPENCODE_IMAGE,
        repo_dir=Path("/tmp/repo"),
        port=4096,
        env={},
        runner=runner,
    )
    serve._container_id = "cid"
    assert serve._scrape_password() == "abc-xyz-123"


def test_start_returns_credential_free_url_and_basic_auth() -> None:
    commands: list[list[str]] = []

    def runner(
        argv: list[str],
        input: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if argv[0] == "docker" and "run" in argv:
            return subprocess.CompletedProcess(argv, returncode=0, stdout="cid\n")
        if argv[0] == "docker" and "logs" in argv:
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout="server password secret-token\n"
            )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    serve = DockerServe(
        image=OPENCODE_IMAGE,
        repo_dir=Path("/tmp/repo"),
        port=4096,
        env={},
        runner=runner,
    )
    base_url = serve.start()

    assert base_url == "http://127.0.0.1:4096"
    assert serve.auth == ("opencode", "secret-token")


async def test_executor_sends_scraped_password_as_basic_auth(tmp_path: Path) -> None:
    authorization_headers: list[str | None] = []

    def docker_runner(
        argv: list[str],
        input: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == "docker" and "run" in argv:
            return subprocess.CompletedProcess(argv, returncode=0, stdout="cid\n")
        if argv[0] == "docker" and "logs" in argv:
            return subprocess.CompletedProcess(
                argv, returncode=0, stdout="server password secret-token\n"
            )
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    base_handler = _handler(events=[], diff_items=[])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session" and request.method == "POST":
            authorization_headers.append(request.headers.get("authorization"))
        return base_handler(request)

    executor = OpencodeExecutor(
        base_url=None,
        workspace=tmp_path,
        model="qwen-coder",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        transport=httpx.MockTransport(handler),
        docker_runner=docker_runner,
    )

    session = await executor.start(_request())
    await session.stop()

    assert authorization_headers == ["Basic b3BlbmNvZGU6c2VjcmV0LXRva2Vu"]


async def test_done_path_releases_resources(tmp_path: Path) -> None:
    events = [
        {
            "id": "evt_1",
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_test"},
        }
    ]
    transport = httpx.MockTransport(_handler(events=events, diff_items=[], status_tokens=None))

    stop_calls: list[None] = []

    class SpyExecutor(OpencodeExecutor):
        async def _stop_serve(self) -> None:
            stop_calls.append(None)

    executor = SpyExecutor(
        base_url="http://localhost:8080",
        workspace=tmp_path,
        model="qwen-coder",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        transport=transport,
    )
    session = cast(OpencodeSession, await executor.start(_request()))
    assert await session.wait_done() == ExecutionStatus.DONE
    await session.stop()

    assert len(stop_calls) == 1
    assert session._http.is_closed


async def test_start_failure_after_container_start_stops_container(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def docker_runner(
        argv: list[str],
        input: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if argv[0] == "docker" and "run" in argv:
            return subprocess.CompletedProcess(argv, returncode=0, stdout="cid\n")
        if argv[0] == "docker" and "logs" in argv:
            return subprocess.CompletedProcess(argv, returncode=0, stdout="server password x\n")
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session" and request.method == "POST":
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    executor = OpencodeExecutor(
        base_url=None,
        workspace=tmp_path,
        model="qwen-coder",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        transport=httpx.MockTransport(handler),
        docker_runner=docker_runner,
    )
    with pytest.raises(Exception):  # noqa: B017
        await executor.start(_request())

    stop_cmd = next((c for c in commands if c[0] == "docker" and "stop" in c), None)
    assert stop_cmd is not None
    assert "cid" in stop_cmd


async def test_unparseable_diff_after_done_becomes_failed(tmp_path: Path) -> None:
    events = [
        {
            "id": "evt_1",
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_test"},
        }
    ]
    # A non-empty diff that contains no file headers is unparseable.
    diff_items = [{"file": "README.md", "patch": "not a real diff", "status": "modified"}]
    transport = httpx.MockTransport(
        _handler(events=events, diff_items=diff_items, status_tokens=None)
    )
    executor = OpencodeExecutor(
        base_url="http://localhost:8080",
        workspace=tmp_path,
        model="qwen-coder",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        transport=transport,
    )
    session = await executor.start(_request())

    assert await session.wait_done() == ExecutionStatus.FAILED
    assert session.result is None

    log_path = _evidence_dir(tmp_path, _request()) / "session_log.ndjson"
    records = map(json.loads, log_path.read_text(encoding="utf-8").splitlines())
    errors = [r for r in records if r["type"] == "harness_error"]
    assert len(errors) == 1


async def test_evidence_root_separates_artifacts_from_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "repo_mount"
    evidence_root = tmp_path / "evidence"
    events = [
        {
            "id": "evt_1",
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_test"},
        }
    ]
    transport = httpx.MockTransport(_handler(events=events, diff_items=[], status_tokens=None))
    executor = OpencodeExecutor(
        base_url="http://localhost:8080",
        workspace=workspace,
        evidence_root=evidence_root,
        model="qwen-coder",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        transport=transport,
    )
    request = _request()
    session = await executor.start(request)
    assert await session.wait_done() == ExecutionStatus.DONE

    result = session.result
    assert result is not None
    expected_evidence = _evidence_dir(evidence_root, request)
    assert result.session_log_path == expected_evidence / "session_log.ndjson"
    assert result.manifest_path == expected_evidence / "manifest.json"
    assert expected_evidence.is_dir()
    # workspace itself is not polluted with evidence
    assert not (workspace / ".agent_runtime").exists()


def test_opencode_config_text_renders_provider_and_permissions() -> None:
    from agent_runtime.opencode_executor import opencode_config_text

    text = opencode_config_text(model="litellm/qwen3", llm_endpoint="http://litellm.internal:4000")
    assert '"model": "litellm/qwen3"' in text
    assert '"baseURL": "http://litellm.internal:4000"' in text
    assert '"apiKey": "{env:LITELLM_API_KEY}"' in text
    assert '"effect": "allow"' in text
    assert text.startswith("{") and text.endswith("\n")


def test_opencode_config_text_registers_remote_mcp_servers() -> None:
    import json

    from agent_runtime.opencode_executor import opencode_config_text

    text = opencode_config_text(
        model="litellm/qwen3",
        llm_endpoint="http://litellm.internal:4000",
        mcp_servers={"tracker": "http://task-source.agents.svc:8000/mcp"},
    )
    config = json.loads(text)
    # Shape verified live against opencode 2.2.0 (`opencode mcp add --url`).
    assert config["mcp"] == {
        "servers": {"tracker": {"type": "remote", "url": "http://task-source.agents.svc:8000/mcp"}}
    }


def test_opencode_config_text_omits_mcp_when_none_or_empty() -> None:
    import json

    from agent_runtime.opencode_executor import opencode_config_text

    empty: dict[str, str] = {}
    for servers in (None, empty):
        text = opencode_config_text(
            model="litellm/qwen3", llm_endpoint="http://l:4000", mcp_servers=servers
        )
        assert "mcp" not in json.loads(text)


async def test_attached_mode_sends_basic_auth(tmp_path: Path) -> None:
    """k3s sandboxes attach with Basic auth scraped from the pod logs."""
    import base64

    import httpx
    from agent_runtime.opencode_executor import OpencodeExecutor

    seen_auth: list[str | None] = []
    terminal = {
        "id": "evt_t",
        "type": "session.execution.succeeded",
        "data": {"sessionID": "ses_test"},
    }
    inner = _handler(events=[terminal], diff_items=[], status_tokens=None)

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization"))
        return inner(request)

    executor = OpencodeExecutor(
        base_url="http://127.0.0.1:30100",
        workspace=tmp_path,
        model="qwen-coder",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        transport=httpx.MockTransport(handler),
        auth=("opencode", "pod-pass"),
    )
    session = await executor.start(_request())
    status = await session.wait_done()
    assert status == ExecutionStatus.DONE
    expected = "Basic " + base64.b64encode(b"opencode:pod-pass").decode()
    assert seen_auth, "at least one request must have been made"
    assert all(h == expected for h in seen_auth)
