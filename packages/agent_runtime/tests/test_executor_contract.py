"""Contract tests for the Executor interface — a Fake must satisfy the protocols."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from agent_runtime.executor import (
    ExecutionEvent,
    ExecutionRequest,
    ExecutionResult,
    ExecutionSession,
    ExecutionStatus,
    Executor,
)
from agent_runtime.session_log import SessionLog

_FIXED_TS = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
_FIXED_EVENT = ExecutionEvent(ts=_FIXED_TS, type="session.text.delta", payload={"delta": "hi"})
_FIXED_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
_FIXED_RESULT = ExecutionResult(
    diff=_FIXED_DIFF,
    diff_files=["x.py"],
    session_log_path=Path("/tmp/session.ndjson"),
    manifest_path=Path("/tmp/manifest.json"),
    tokens_used=123,
)


class FakeSession:
    """Session with fixed events/diff/status/result for contract checks."""

    def __init__(self) -> None:
        self.stopped = False
        self._session_log = SessionLog(_FIXED_RESULT.session_log_path)

    async def events(self) -> AsyncIterator[ExecutionEvent]:
        yield _FIXED_EVENT

    async def wait_done(self) -> ExecutionStatus:
        return ExecutionStatus.DONE

    async def diff(self) -> str:
        return _FIXED_DIFF

    async def status(self) -> ExecutionStatus:
        return ExecutionStatus.DONE

    async def stop(self) -> None:
        self.stopped = True

    @property
    def result(self) -> ExecutionResult | None:
        return _FIXED_RESULT

    @property
    def session_log(self) -> SessionLog:
        return self._session_log


class FakeExecutor:
    """Executor that returns a single canned session."""

    def __init__(self) -> None:
        self.session = FakeSession()

    async def start(self, request: ExecutionRequest) -> ExecutionSession:
        return self.session


def _request() -> ExecutionRequest:
    return ExecutionRequest(
        run_id=1,
        vertex_id=2,
        name="add-feature",
        prompt="Implement X with tests",
        paths_allow=["src/**", "tests/**"],
        done_when='{"checks": [{"type": "cmd", "run": "pytest -q"}]}',
        repo_url="http://gitea.local/swarm-lab/target-scratch.git",
        work_branch="agent/vertex-2",
    )


async def test_fake_executor_satisfies_contract() -> None:
    fake = FakeExecutor()
    assert isinstance(fake, Executor)

    session = await fake.start(_request())
    assert isinstance(session, ExecutionSession)

    assert await session.wait_done() == ExecutionStatus.DONE
    assert await session.status() == ExecutionStatus.DONE
    assert await session.diff() == _FIXED_DIFF
    assert session.result == _FIXED_RESULT

    events = [event async for event in session.events()]
    assert events == [_FIXED_EVENT]

    request = _request()
    assert ExecutionRequest.model_validate(request.model_dump()) == request
