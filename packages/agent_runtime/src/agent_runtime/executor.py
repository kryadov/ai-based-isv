"""Executor contract: the harness-agnostic interface of the agent runtime.

Types and protocols only — no behavior lives here. The OpenCode adapter
(Tasks 6-7 of the M4a plan) implements ``Executor``; the worker and tests
consume these models. Design: spec 2026-11-09-m4-agent-runtime-design.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from agent_runtime.session_log import SessionLog


class ExecutionStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"


class ExecutionRequest(BaseModel):
    """A single vertex to execute: task payload plus repo coordinates."""

    run_id: int
    vertex_id: int
    name: str
    prompt: str
    paths_allow: list[str]
    done_when: str
    repo_url: str
    base_branch: str = "main"
    work_branch: str


class ExecutionEvent(BaseModel):
    """One event proxied from the harness stream (SSE of ``opencode serve``)."""

    ts: datetime
    type: str
    payload: dict[str, Any]


class ExecutionResult(BaseModel):
    """Terminal outcome of a session: diff, evidence paths, token usage."""

    diff: str
    diff_files: list[str]
    session_log_path: Path
    manifest_path: Path
    tokens_used: int | None = None


@runtime_checkable
class ExecutionSession(Protocol):
    """A running execution handle: stream, terminal status, evidence."""

    def events(self) -> AsyncIterator[ExecutionEvent]: ...

    async def wait_done(self) -> ExecutionStatus: ...

    async def diff(self) -> str: ...

    async def status(self) -> ExecutionStatus: ...

    async def stop(self) -> None: ...

    @property
    def result(self) -> ExecutionResult | None: ...

    @property
    def session_log(self) -> SessionLog: ...


@runtime_checkable
class Executor(Protocol):
    """Starts an execution session for a request."""

    async def start(self, request: ExecutionRequest) -> ExecutionSession: ...
