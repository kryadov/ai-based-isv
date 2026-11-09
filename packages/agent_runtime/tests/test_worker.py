"""Tests for the run-scoped vertex worker over agent_api."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from agent_runtime.executor import (
    ExecutionEvent,
    ExecutionRequest,
    ExecutionResult,
    ExecutionSession,
    ExecutionStatus,
)
from agent_runtime.session_log import SessionLog
from agent_runtime.settings import WorkerSettings
from agent_runtime.worker import worker_loop

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@localhost",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@localhost",
}


def _init_bare_repo(tmp_path: Path) -> Path:
    """Create a bare repo with a ``main`` branch containing ``README.md``."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "origin_work"
    work.mkdir(parents=True)
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True, env=_GIT_ENV)
    (work / "README.md").write_text("# base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work), "add", "."],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "-C", str(work), "clone", "--bare", ".", str(bare)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    return bare


class FakeSession:
    """In-memory execution session for worker tests."""

    def __init__(
        self,
        request: ExecutionRequest,
        *,
        diff: str,
        result: ExecutionResult,
        wait_done_status: ExecutionStatus = ExecutionStatus.DONE,
        block_until_stop: bool = False,
    ) -> None:
        self._request = request
        self._diff = diff
        self._result = result
        self._wait_done_status = wait_done_status
        self._block_until_stop = block_until_stop
        self.stopped = False
        self._stop_event = asyncio.Event()

    async def events(self) -> AsyncIterator[ExecutionEvent]:
        yield ExecutionEvent(
            ts=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
            type="test.event",
            payload={"vertex_id": self._request.vertex_id},
        )

    async def wait_done(self) -> ExecutionStatus:
        if self._block_until_stop:
            await self._stop_event.wait()
        return self._wait_done_status

    async def diff(self) -> str:
        return self._diff

    async def status(self) -> ExecutionStatus:
        return self._wait_done_status

    async def stop(self) -> None:
        self.stopped = True
        self._stop_event.set()

    @property
    def result(self) -> ExecutionResult | None:
        return self._result

    @property
    def session_log(self) -> SessionLog:
        return SessionLog(self._result.session_log_path)


class FakeExecutor:
    """Configurable executor stub for worker tests."""

    def __init__(
        self,
        *,
        diff: str = "",
        diff_files: list[str] | None = None,
        tokens_used: int | None = None,
        exception: BaseException | None = None,
        wait_done_status: ExecutionStatus = ExecutionStatus.DONE,
        block_until_stop: bool = False,
    ) -> None:
        self.diff = diff
        self.diff_files = diff_files or []
        self.tokens_used = tokens_used
        self.exception = exception
        self.wait_done_status = wait_done_status
        self.block_until_stop = block_until_stop
        self.calls: list[ExecutionRequest] = []
        self.sessions: list[FakeSession] = []

    async def start(self, request: ExecutionRequest) -> ExecutionSession:
        self.calls.append(request)
        if self.exception is not None:
            raise self.exception
        evidence = Path(f"/tmp/fake-evidence/run-{request.run_id}/vertex-{request.vertex_id}")
        evidence.mkdir(parents=True, exist_ok=True)
        log_path = evidence / "session_log.ndjson"
        manifest_path = evidence / "manifest.json"
        manifest_path.write_text(json.dumps({"vertex_id": request.vertex_id}), encoding="utf-8")
        result = ExecutionResult(
            diff=self.diff,
            diff_files=self.diff_files,
            session_log_path=log_path,
            manifest_path=manifest_path,
            tokens_used=self.tokens_used,
        )
        session = FakeSession(
            request,
            diff=self.diff,
            result=result,
            wait_done_status=self.wait_done_status,
            block_until_stop=self.block_until_stop,
        )
        self.sessions.append(session)
        return session


class FakeControlPlane:
    """Stateful httpx MockTransport backend for the agent_api control plane."""

    def __init__(
        self,
        vertices: list[dict[str, Any]] | None = None,
        heartbeat_409_after: int = 0,
    ) -> None:
        self.vertices = list(vertices or [])
        self.heartbeat_409_after = heartbeat_409_after
        self.claim_index = 0
        self.claim_calls = 0
        self.heartbeat_count = 0
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.complete_calls: list[tuple[int, dict[str, Any]]] = []
        self.fail_calls: list[tuple[int, dict[str, Any]]] = []
        self.audit_calls: list[dict[str, Any]] = []
        self.complete_called = asyncio.Event()
        self.fail_called = asyncio.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body: dict[str, Any] = {}
        if request.content:
            body = json.loads(request.content.decode("utf-8"))
        self.requests.append((method, path, body))

        if path == "/vertices/claim" and method == "POST":
            self.claim_calls += 1
            if self.claim_index < len(self.vertices):
                vertex = self.vertices[self.claim_index]
                self.claim_index += 1
                return httpx.Response(200, json={"vertex": vertex})
            return httpx.Response(200, json={"vertex": None})

        heartbeat_match = re.match(r"/vertices/(\d+)/heartbeat", path)
        if heartbeat_match:
            self.heartbeat_count += 1
            if self.heartbeat_409_after and self.heartbeat_count >= self.heartbeat_409_after:
                return httpx.Response(409, json={"detail": "vertex not leased by agent"})
            return httpx.Response(200, json={"ok": True})

        complete_match = re.match(r"/vertices/(\d+)/complete", path)
        if complete_match:
            vertex_id = int(complete_match.group(1))
            self.complete_calls.append((vertex_id, body))
            self.complete_called.set()
            return httpx.Response(200, json={"ok": True})

        fail_match = re.match(r"/vertices/(\d+)/fail", path)
        if fail_match:
            vertex_id = int(fail_match.group(1))
            self.fail_calls.append((vertex_id, body))
            self.fail_called.set()
            return httpx.Response(200, json={"ok": True, "requeued": True})

        if path == "/audit" and method == "POST":
            self.audit_calls.append(body)
            return httpx.Response(200, json={"event_id": len(self.audit_calls)})

        return httpx.Response(404)


def _settings(tmp_path: Path, repo_url: str) -> WorkerSettings:
    return WorkerSettings(
        api_url="http://test",
        run_id=42,
        agent="test-worker",
        poll_interval_seconds=0.05,
        lease_ttl_seconds=300,
        workspace_dir=tmp_path / "workspace",
        repo_url=repo_url,
        base_branch="main",
        litellm_base_url="http://litellm.test/v1",
        model="litellm/qwen-coder",
    )


async def test_happy_path_claims_executes_completes(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    diff = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,2 @@\n"
        " # base\n"
        "+hello\n"
    )
    executor = FakeExecutor(
        diff=diff,
        diff_files=["README.md"],
        tokens_used=42,
    )
    vertex = {
        "id": 1,
        "name": "greet",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["README.md"]}]}),
        "paths_allow": ["README.md"],
        "prompt": "Say hello",
    }
    plane = FakeControlPlane(vertices=[vertex])
    settings = _settings(tmp_path, str(bare))
    stop = asyncio.Event()

    transport = httpx.MockTransport(plane.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        task = asyncio.create_task(worker_loop(settings, executor, http=http, stop=stop))
        await asyncio.wait_for(plane.complete_called.wait(), timeout=2.0)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(plane.complete_calls) == 1
    vertex_id, result = plane.complete_calls[0]
    assert vertex_id == 1
    assert result["agent"] == "test-worker"
    assert result["result"]["branch"] == "agent/vertex-1"
    assert result["result"]["diff_files"] == ["README.md"]
    assert result["result"]["verdict"]["passed"] is True
    assert result["result"]["session_log_path"]
    assert result["result"]["manifest_path"]
    assert result["result"]["tokens_used"] == 42

    assert len(executor.calls) == 1
    request = executor.calls[0]
    assert request.vertex_id == 1
    assert request.work_branch == "agent/vertex-1"


async def test_done_when_unparseable_fails_vertex(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    executor = FakeExecutor(diff="", diff_files=[])
    vertex = {
        "id": 2,
        "name": "broken",
        "done_when": "junk",
        "paths_allow": ["*.md"],
        "prompt": "do something",
    }
    plane = FakeControlPlane(vertices=[vertex])
    settings = _settings(tmp_path, str(bare))
    stop = asyncio.Event()

    transport = httpx.MockTransport(plane.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        task = asyncio.create_task(worker_loop(settings, executor, http=http, stop=stop))
        await asyncio.wait_for(plane.fail_called.wait(), timeout=2.0)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(plane.fail_calls) == 1
    vertex_id, body = plane.fail_calls[0]
    assert vertex_id == 2
    assert body["reason"] == "unparseable_done_when"
    assert len(plane.complete_calls) == 0


async def test_paths_violation_fails_vertex(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    diff = (
        "diff --git a/secrets.env b/secrets.env\n"
        "--- a/secrets.env\n"
        "+++ b/secrets.env\n"
        "@@ -0,0 +1 @@\n"
        "+leak\n"
    )
    executor = FakeExecutor(diff=diff, diff_files=["secrets.env"])
    vertex = {
        "id": 3,
        "name": "leaker",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["README.md"]}]}),
        "paths_allow": ["src/**"],
        "prompt": "do something",
    }
    plane = FakeControlPlane(vertices=[vertex])
    settings = _settings(tmp_path, str(bare))
    stop = asyncio.Event()

    transport = httpx.MockTransport(plane.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        task = asyncio.create_task(worker_loop(settings, executor, http=http, stop=stop))
        await asyncio.wait_for(plane.fail_called.wait(), timeout=2.0)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(plane.fail_calls) == 1
    vertex_id, body = plane.fail_calls[0]
    assert vertex_id == 3
    assert "paths_violation" in body["reason"]
    assert "secrets.env" in body["reason"]
    assert len(plane.complete_calls) == 0


async def test_heartbeat_conflict_aborts_without_complete(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    executor = FakeExecutor(diff="", diff_files=[], block_until_stop=True)
    vertex = {
        "id": 4,
        "name": "abandoned",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["README.md"]}]}),
        "paths_allow": ["*.md"],
        "prompt": "do something",
    }
    plane = FakeControlPlane(vertices=[vertex], heartbeat_409_after=1)
    settings = _settings(tmp_path, str(bare))
    settings.lease_ttl_seconds = 1  # keep interval tiny at ~0.33s
    stop = asyncio.Event()

    transport = httpx.MockTransport(plane.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        task = asyncio.create_task(worker_loop(settings, executor, http=http, stop=stop))
        # wait for the heartbeat conflict and session teardown
        deadline = asyncio.get_event_loop().time() + 2.0
        while (
            plane.heartbeat_count < 1
            or not executor.sessions[-1].stopped
        ):
            await asyncio.sleep(0.01)
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("heartbeat conflict did not happen")
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(plane.complete_calls) == 0
    assert len(plane.fail_calls) == 0
    assert executor.sessions[-1].stopped


async def test_executor_exception_fails_vertex(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    executor = FakeExecutor(exception=RuntimeError("boom"))
    vertex = {
        "id": 5,
        "name": "exploder",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["README.md"]}]}),
        "paths_allow": ["*.md"],
        "prompt": "do something",
    }
    plane = FakeControlPlane(vertices=[vertex])
    settings = _settings(tmp_path, str(bare))
    stop = asyncio.Event()

    transport = httpx.MockTransport(plane.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        task = asyncio.create_task(worker_loop(settings, executor, http=http, stop=stop))
        await asyncio.wait_for(plane.fail_called.wait(), timeout=2.0)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(plane.fail_calls) == 1
    vertex_id, body = plane.fail_calls[0]
    assert vertex_id == 5
    assert "RuntimeError" in body["reason"]
    assert len(plane.complete_calls) == 0


async def test_diff_unparseable_fails_vertex(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    executor = FakeExecutor(diff="bogus\n+line\n", diff_files=[])
    vertex = {
        "id": 7,
        "name": "garbage-diff",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["README.md"]}]}),
        "paths_allow": ["*.md"],
        "prompt": "do something",
    }
    plane = FakeControlPlane(vertices=[vertex])
    settings = _settings(tmp_path, str(bare))
    stop = asyncio.Event()

    transport = httpx.MockTransport(plane.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        task = asyncio.create_task(worker_loop(settings, executor, http=http, stop=stop))
        await asyncio.wait_for(plane.fail_called.wait(), timeout=2.0)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(plane.fail_calls) == 1
    vertex_id, body = plane.fail_calls[0]
    assert vertex_id == 7
    assert "diff_unparseable" in body["reason"]
    assert len(plane.complete_calls) == 0


async def test_empty_claim_sleeps(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    executor = FakeExecutor()
    plane = FakeControlPlane(vertices=[])
    settings = _settings(tmp_path, str(bare))
    stop = asyncio.Event()

    transport = httpx.MockTransport(plane.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        task = asyncio.create_task(worker_loop(settings, executor, http=http, stop=stop))
        await asyncio.sleep(0.15)
        assert plane.claim_calls >= 1
        assert len(plane.complete_calls) == 0
        assert len(plane.fail_calls) == 0
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(plane.complete_calls) == 0
    assert len(plane.fail_calls) == 0
