"""Tests for the run-scoped vertex worker over agent_api."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import agent_runtime.worker as worker_module
import httpx
import pytest
from agent_runtime.done_when import DoneWhenSpec, DoneWhenVerdict
from agent_runtime.executor import (
    ExecutionEvent,
    ExecutionRequest,
    ExecutionResult,
    ExecutionSession,
    ExecutionStatus,
)
from agent_runtime.session_log import SessionLog
from agent_runtime.settings import WorkerSettings
from agent_runtime.worker import build_vertex_prompt, worker_loop

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
    subprocess.run(
        ["git", "init", "-b", "main", str(work)],
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
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
        claim_errors: int = 0,
    ) -> None:
        self.vertices = list(vertices or [])
        self.heartbeat_409_after = heartbeat_409_after
        self.claim_errors = claim_errors
        self.claim_index = 0
        self.claim_calls = 0
        self.heartbeat_count = 0
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.complete_calls: list[tuple[int, dict[str, Any]]] = []
        self.fail_calls: list[tuple[int, dict[str, Any]]] = []
        self.audit_calls: list[dict[str, Any]] = []
        self.complete_called = asyncio.Event()
        self.fail_called = asyncio.Event()
        self.heartbeat_called = asyncio.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body: dict[str, Any] = {}
        if request.content:
            body = json.loads(request.content.decode("utf-8"))
        self.requests.append((method, path, body))

        if path == "/vertices/claim" and method == "POST":
            self.claim_calls += 1
            if self.claim_errors > 0:
                self.claim_errors -= 1
                return httpx.Response(500, json={"detail": "control plane outage"})
            if self.claim_index < len(self.vertices):
                vertex = self.vertices[self.claim_index]
                self.claim_index += 1
                return httpx.Response(200, json={"vertex": vertex})
            return httpx.Response(200, json={"vertex": None})

        heartbeat_match = re.match(r"/vertices/(\d+)/heartbeat", path)
        if heartbeat_match:
            self.heartbeat_count += 1
            self.heartbeat_called.set()
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
    # The diff creates a new file; done_when checks that file exists AFTER apply.
    diff = (
        "diff --git a/greeting.py b/greeting.py\n"
        "new file mode 100644\n"
        "index 0000000..d8649da\n"
        "--- /dev/null\n"
        "+++ b/greeting.py\n"
        "@@ -0,0 +1 @@\n"
        "+print('hello')\n"
    )
    executor = FakeExecutor(
        diff=diff,
        diff_files=["greeting.py"],
        tokens_used=42,
    )
    vertex = {
        "id": 1,
        "name": "greet",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["greeting.py"]}]}),
        "paths_allow": ["*.py"],
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
    assert result["result"]["diff_files"] == ["greeting.py"]
    assert result["result"]["verdict"]["passed"] is True
    assert result["result"]["session_log_path"]
    assert result["result"]["manifest_path"]
    assert result["result"]["tokens_used"] == 42

    assert len(executor.calls) == 1
    request = executor.calls[0]
    assert request.vertex_id == 1
    assert request.work_branch == "agent/vertex-1"
    assert "Task: greet" in request.prompt
    assert "Done when:" in request.prompt
    assert "Do not push or commit" in request.prompt

    # The sandbox copy was created and is isolated from the pristine clone.
    run_ws = tmp_path / "workspace" / "run-42"
    assert (run_ws / "sandbox" / ".git").is_dir()

    # The pristine clone received the pushed work branch.
    repo_dir = run_ws / "origin"  # ensure_repo strips the .git suffix
    assert repo_dir.is_dir()
    base_sha = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
        env=_GIT_ENV,
    ).stdout.strip()
    branch_sha = subprocess.run(
        ["git", "rev-parse", "agent/vertex-1"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
        env=_GIT_ENV,
    ).stdout.strip()
    assert branch_sha != base_sha
    assert (repo_dir / "greeting.py").exists()


async def test_done_when_validation_does_not_block_heartbeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = _init_bare_repo(tmp_path)
    diff = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,2 @@\n"
        " # base\n"
        "+change\n"
    )
    executor = FakeExecutor(diff=diff, diff_files=["README.md"])
    vertex = {
        "id": 9,
        "name": "slow-validation",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["README.md"]}]}),
        "paths_allow": ["*.md"],
    }
    plane = FakeControlPlane(vertices=[vertex])
    settings = _settings(tmp_path, str(bare))
    settings.lease_ttl_seconds = 1
    stop = asyncio.Event()
    validation_started = threading.Event()
    release_validation = threading.Event()
    heartbeat_during_validation = asyncio.Event()

    def blocking_validate(spec: DoneWhenSpec, *, cwd: Path) -> DoneWhenVerdict:
        validation_started.set()
        assert release_validation.wait(timeout=5.0)
        return DoneWhenVerdict(passed=True, results=[])

    monkeypatch.setattr(worker_module, "validate_done_when", blocking_validate)
    release_timer = threading.Timer(2.0, release_validation.set)
    release_timer.start()

    def handler(request: httpx.Request) -> httpx.Response:
        response = plane.handler(request)
        if (
            request.url.path.endswith("/heartbeat")
            and validation_started.is_set()
            and not release_validation.is_set()
        ):
            heartbeat_during_validation.set()
        return response

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        task = asyncio.create_task(worker_loop(settings, executor, http=http, stop=stop))
        try:
            assert await asyncio.to_thread(validation_started.wait, 1.0)
            await asyncio.wait_for(heartbeat_during_validation.wait(), timeout=1.0)
            release_validation.set()
            await asyncio.wait_for(plane.complete_called.wait(), timeout=2.0)
        finally:
            release_validation.set()
            release_timer.cancel()
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)

    assert plane.heartbeat_count >= 1


async def test_done_when_unparseable_fails_vertex(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    # Use a valid, applyable diff so we reach the done_when check.
    diff = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,2 @@\n"
        " # base\n"
        "+junk\n"
    )
    executor = FakeExecutor(diff=diff, diff_files=["README.md"])
    vertex = {
        "id": 2,
        "name": "broken",
        "done_when": "junk",
        "paths_allow": ["*.md"],
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
    # Valid new-file diff; the path is outside the allowed scope.
    diff = (
        "diff --git a/secrets.env b/secrets.env\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n"
        "--- /dev/null\n"
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


async def test_diff_unparseable_fails_vertex(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    executor = FakeExecutor(diff="bogus\n+line\n", diff_files=[])
    vertex = {
        "id": 7,
        "name": "garbage-diff",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["README.md"]}]}),
        "paths_allow": ["*.md"],
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


async def test_heartbeat_conflict_aborts_without_complete(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    executor = FakeExecutor(diff="", diff_files=[], block_until_stop=True)
    vertex = {
        "id": 4,
        "name": "abandoned",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["README.md"]}]}),
        "paths_allow": ["*.md"],
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
        while plane.heartbeat_count < 1 or not executor.sessions[-1].stopped:
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


async def test_empty_diff_fails_vertex_and_counts_attempts(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    executor = FakeExecutor(diff="", diff_files=[])
    vertex = {
        "id": 6,
        "name": "no-changes",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["README.md"]}]}),
        "paths_allow": ["*.md"],
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
    assert vertex_id == 6
    assert body["reason"] == "empty_diff"
    assert len(plane.complete_calls) == 0


async def test_max_attempts_guard_blacklists_vertex(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    executor = FakeExecutor(diff="", diff_files=[])
    vertex = {
        "id": 8,
        "name": "tired",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["README.md"]}]}),
        "paths_allow": ["*.md"],
        "attempts": 3,
    }
    # The vertex is returned twice; the worker must fail it once and ignore it forever.
    plane = FakeControlPlane(vertices=[vertex, vertex])
    settings = _settings(tmp_path, str(bare))
    stop = asyncio.Event()

    transport = httpx.MockTransport(plane.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        task = asyncio.create_task(worker_loop(settings, executor, http=http, stop=stop))
        await asyncio.wait_for(plane.fail_called.wait(), timeout=2.0)
        await asyncio.sleep(0.15)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert len(plane.fail_calls) == 1
    vertex_id, body = plane.fail_calls[0]
    assert vertex_id == 8
    assert body["reason"] == "max_attempts_exceeded"
    assert plane.claim_calls >= 2


async def test_claim_error_is_resilient(tmp_path: Path) -> None:
    bare = _init_bare_repo(tmp_path)
    executor = FakeExecutor()
    plane = FakeControlPlane(vertices=[], claim_errors=2)
    settings = _settings(tmp_path, str(bare))
    stop = asyncio.Event()

    transport = httpx.MockTransport(plane.handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        task = asyncio.create_task(worker_loop(settings, executor, http=http, stop=stop))
        await asyncio.sleep(0.15)
        assert plane.claim_calls >= 2
        assert len(plane.complete_calls) == 0
        assert len(plane.fail_calls) == 0
        assert any(call.get("action") == "claim_error" for call in plane.audit_calls)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)


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


async def test_build_vertex_prompt_uses_name_scope_and_done_when() -> None:
    vertex = {
        "name": "add-feature",
        "paths_allow": ["src/**", "tests/**"],
        "done_when": json.dumps({"checks": [{"type": "cmd", "run": "pytest"}]}),
    }
    prompt = build_vertex_prompt(vertex)
    assert "Task: add-feature" in prompt
    assert "Allowed paths: src/**, tests/**" in prompt
    assert '"checks"' in prompt
    assert "Do not push or commit" in prompt


# --- Tracker status loop (M4c Task 5) ------------------------------------------

_GREETING_DIFF = (
    "diff --git a/greeting.py b/greeting.py\n"
    "new file mode 100644\n"
    "index 0000000..d8649da\n"
    "--- /dev/null\n"
    "+++ b/greeting.py\n"
    "@@ -0,0 +1 @@\n"
    "+print('hello')\n"
)


class FakeTracker:
    """task-source MCP endpoint: records tools/call requests, can be down."""

    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        params = body["params"]
        self.calls.append((params["name"], params["arguments"]))
        if self.down:
            return httpx.Response(503, text="tracker outage")
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [], "isError": False}},
        )

    def statuses(self) -> list[tuple[str, str]]:
        return [(a["status"], a.get("note", "")) for n, a in self.calls if n == "set_task_status"]


async def _run_with_tracker(
    tmp_path: Path,
    *,
    executor: FakeExecutor,
    vertex: dict[str, Any],
    tracker: FakeTracker,
    task_ref: str = "swarm-lab/target-scratch#7",
    wait_for: str = "complete",
) -> FakeControlPlane:
    from agent_runtime.tracker import TrackerClient

    bare = _init_bare_repo(tmp_path)
    plane = FakeControlPlane(vertices=[vertex])
    settings = _settings(tmp_path, str(bare)).model_copy(
        update={"task_ref": task_ref, "task_source_url": "http://tracker.test/mcp"}
    )
    stop = asyncio.Event()
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(plane.handler), base_url="http://test"
        ) as http,
        httpx.AsyncClient(transport=httpx.MockTransport(tracker.handler)) as tracker_http,
    ):
        client = TrackerClient(tracker_http, "http://tracker.test/mcp")
        task = asyncio.create_task(
            worker_loop(settings, executor, http=http, stop=stop, tracker=client)
        )
        event = plane.complete_called if wait_for == "complete" else plane.fail_called
        await asyncio.wait_for(event.wait(), timeout=3.0)
        # Let the post-complete/fail tracker call land before stopping.
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=3.0)
    return plane


def _greet_vertex(vertex_id: int = 1) -> dict[str, Any]:
    return {
        "id": vertex_id,
        "name": "greet",
        "done_when": json.dumps({"checks": [{"type": "files_exist", "paths": ["greeting.py"]}]}),
        "paths_allow": ["*.py"],
    }


async def test_tracker_claim_in_progress_then_done_with_branch_and_verdict(
    tmp_path: Path,
) -> None:
    tracker = FakeTracker()
    executor = FakeExecutor(diff=_GREETING_DIFF, diff_files=["greeting.py"])
    plane = await _run_with_tracker(
        tmp_path, executor=executor, vertex=_greet_vertex(), tracker=tracker
    )

    assert len(plane.complete_calls) == 1
    statuses = tracker.statuses()
    assert [s for s, _ in statuses] == ["in-progress", "done"]
    assert all(a["task_ref"] == "swarm-lab/target-scratch#7" for _, a in tracker.calls)
    done_note = statuses[1][1]
    assert "agent/vertex-1" in done_note
    assert "verdict: PASS" in done_note


async def test_tracker_fail_sets_blocked_with_reason(tmp_path: Path) -> None:
    tracker = FakeTracker()
    executor = FakeExecutor(diff="", diff_files=[])
    plane = await _run_with_tracker(
        tmp_path, executor=executor, vertex=_greet_vertex(6), tracker=tracker, wait_for="fail"
    )
    assert plane.fail_calls[0][1]["reason"] == "empty_diff"
    statuses = tracker.statuses()
    assert [s for s, _ in statuses] == ["in-progress", "blocked"]
    assert "empty_diff" in statuses[1][1]


async def test_tracker_outage_never_fails_the_vertex(tmp_path: Path) -> None:
    tracker = FakeTracker(down=True)
    executor = FakeExecutor(diff=_GREETING_DIFF, diff_files=["greeting.py"])
    plane = await _run_with_tracker(
        tmp_path, executor=executor, vertex=_greet_vertex(), tracker=tracker
    )

    assert len(plane.complete_calls) == 1
    assert plane.fail_calls == []
    # The outage is visible: audited, not silent.
    tracker_errors = [a for a in plane.audit_calls if a["action"] == "tracker_error"]
    assert len(tracker_errors) == 2  # in-progress and done both failed
    assert all(a["outcome"] == "error" for a in tracker_errors)
    assert "in-progress" in json.dumps(tracker_errors[0]["payload"])


async def test_empty_task_ref_disables_the_tracker_loop(tmp_path: Path) -> None:
    tracker = FakeTracker()
    executor = FakeExecutor(diff=_GREETING_DIFF, diff_files=["greeting.py"])
    plane = await _run_with_tracker(
        tmp_path, executor=executor, vertex=_greet_vertex(), tracker=tracker, task_ref=""
    )
    assert len(plane.complete_calls) == 1
    assert tracker.calls == []
