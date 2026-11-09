"""Run-scoped vertex worker: claim vertices from agent_api, execute them, report back.

The worker loop is the integration seam between the control plane (M3), the
executor adapter (Task 7), gitops (Task 8) and validation (Task 4). It is
intentionally plain asyncio + httpx: no FastAPI/SQLAlchemy inside the runtime
package.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any, cast

import httpx

from agent_runtime.done_when import (
    DoneWhenUnparseable,
    DoneWhenVerdict,
    parse_done_when,
    validate_done_when,
)
from agent_runtime.executor import ExecutionRequest, ExecutionSession, ExecutionStatus, Executor
from agent_runtime.gitops import apply_diff, ensure_repo, push_head
from agent_runtime.manifest import write_manifest
from agent_runtime.opencode_executor import OpencodeExecutor
from agent_runtime.pathguard import DiffUnparseable, diff_files, filter_violations
from agent_runtime.settings import WorkerSettings


class ControlPlaneClient:
    """Thin async client for the agent_api vertex lifecycle endpoints."""

    def __init__(self, http: httpx.AsyncClient, settings: WorkerSettings) -> None:
        self._http = http
        self._settings = settings

    async def claim(self) -> dict[str, Any] | None:
        response = await self._http.post(
            "/vertices/claim",
            json={
                "run_id": self._settings.run_id,
                "agent": self._settings.agent,
                "ttl_seconds": self._settings.lease_ttl_seconds,
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return cast(dict[str, Any] | None, data.get("vertex"))

    async def heartbeat(self, vertex_id: int) -> bool:
        response = await self._http.post(
            f"/vertices/{vertex_id}/heartbeat",
            json={
                "agent": self._settings.agent,
                "ttl_seconds": self._settings.lease_ttl_seconds,
            },
        )
        return response.status_code == 200

    async def complete(self, vertex_id: int, result: dict[str, Any]) -> None:
        response = await self._http.post(
            f"/vertices/{vertex_id}/complete",
            json={"agent": self._settings.agent, "result": result},
        )
        response.raise_for_status()

    async def fail(self, vertex_id: int, reason: str | None = None) -> None:
        payload: dict[str, Any] = {"agent": self._settings.agent}
        if reason is not None:
            payload["reason"] = reason
        response = await self._http.post(
            f"/vertices/{vertex_id}/fail",
            json=payload,
        )
        response.raise_for_status()

    async def audit(
        self,
        *,
        action: str,
        target: str,
        outcome: str = "ok",
        payload: dict[str, Any] | None = None,
    ) -> None:
        body = {
            "actor": self._settings.agent,
            "action": action,
            "target": target,
            "outcome": outcome,
            "payload": payload,
        }
        with contextlib.suppress(Exception):
            await self._http.post("/audit", json=body)


def _git_token(raw: str) -> str | None:
    return raw if raw else None


def _run_workspace(settings: WorkerSettings) -> Path:
    return settings.workspace_dir / f"run-{settings.run_id}"


def _repo_dir_name(repo_url: str) -> str:
    """Derive a stable local directory name from a remote URL or path."""
    tail = repo_url.rstrip("/").rsplit("/", 1)[-1]
    tail = tail.rsplit(":", 1)[-1]
    name = tail[: -len(".git")] if tail.endswith(".git") else tail
    return name or "repo"


def build_vertex_prompt(vertex: dict[str, Any]) -> str:
    """Assemble the prompt the sandbox receives from the vertex metadata."""
    name = vertex.get("name") or "unnamed"
    allowed = ", ".join(vertex.get("paths_allow", [])) or "(none)"
    done_when = vertex.get("done_when", "")
    return (
        f"Task: {name}\n"
        f"Allowed paths: {allowed}\n"
        f"Done when: {done_when}\n\n"
        "Work in the current repository directory. Make the requested changes; "
        "the worker will collect them from the session diff. Do not push or commit."
    )


def _build_result(
    session: ExecutionSession,
    changed: list[str],
    verdict: DoneWhenVerdict,
    branch: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "branch": branch,
        "diff_files": changed,
        "verdict": verdict.model_dump(),
    }
    execution_result = session.result
    if execution_result is not None:
        result["session_log_path"] = str(execution_result.session_log_path)
        result["manifest_path"] = str(execution_result.manifest_path)
        result["tokens_used"] = execution_result.tokens_used
        _merge_manifest(execution_result.manifest_path, verdict.model_dump())
    else:
        result["session_log_path"] = str(session.session_log.path)
        result["manifest_path"] = ""
        result["tokens_used"] = None
    return result


def _merge_manifest(manifest_path: Path, verdict: dict[str, Any]) -> None:
    if not manifest_path.exists():
        return
    manifest: dict[str, Any] = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )
    manifest["verdict"] = verdict
    write_manifest(manifest_path, manifest)


async def _heartbeat_loop(
    client: ControlPlaneClient,
    vertex_id: int,
    lease_ttl: float,
    lease_lost: asyncio.Event,
    stop: asyncio.Event,
) -> None:
    """Heartbeat every ``lease_ttl/3`` seconds until cancelled or lease lost."""
    interval = lease_ttl / 3
    try:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                ok = await client.heartbeat(vertex_id)
            except Exception:
                # Transport-level failure: treat as lease lost and abandon the vertex.
                ok = False
            if not ok:
                lease_lost.set()
                return
    except asyncio.CancelledError:
        raise


async def _process_vertex(
    settings: WorkerSettings,
    executor: Executor,
    client: ControlPlaneClient,
    vertex: dict[str, Any],
    stop: asyncio.Event,
) -> None:
    vertex_id: int = vertex["id"]
    session: ExecutionSession | None = None
    heartbeat_task: asyncio.Task[None] | None = None

    try:
        await client.audit(
            action="claim",
            target=f"vertex:{vertex_id}",
            payload={"name": vertex.get("name"), "run_id": settings.run_id},
        )

        # Start heartbeating before any potentially long-running setup work.
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(
                client,
                vertex_id,
                float(settings.lease_ttl_seconds),
                lease_lost,
                stop,
            )
        )

        repo_dir = await asyncio.to_thread(
            ensure_repo,
            _run_workspace(settings),
            settings.repo_url,
            settings.base_branch,
            token=_git_token(settings.git_token),
        )

        request = ExecutionRequest(
            run_id=settings.run_id,
            vertex_id=vertex_id,
            name=vertex["name"],
            prompt=build_vertex_prompt(vertex),
            paths_allow=list(vertex.get("paths_allow", [])),
            done_when=vertex.get("done_when", ""),
            repo_url=settings.repo_url,
            base_branch=settings.base_branch,
            work_branch=f"agent/vertex-{vertex_id}",
        )

        session = await executor.start(request)

        wait_task = asyncio.create_task(session.wait_done())
        lease_wait = asyncio.create_task(lease_lost.wait())
        done, pending = await asyncio.wait(
            {wait_task, lease_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if lease_lost.is_set():
            # Lease lost: do NOT complete or fail. The control plane will
            # requeue via lease expiry.
            return

        status = wait_task.result()
        if status != ExecutionStatus.DONE:
            reason = f"session_status:{status.value}"
            await client.fail(vertex_id, reason=reason)
            await client.audit(
                action="fail",
                target=f"vertex:{vertex_id}",
                outcome="error",
                payload={"reason": reason},
            )
            return

        diff = await session.diff()

        try:
            await asyncio.to_thread(
                apply_diff,
                repo_dir,
                diff,
                token=_git_token(settings.git_token),
            )
        except Exception as exc:
            reason = (
                "empty_diff"
                if "empty diff" in str(exc).lower()
                else f"apply_diff_failed: {exc}"
            )
            await client.fail(vertex_id, reason=reason)
            await client.audit(
                action="fail",
                target=f"vertex:{vertex_id}",
                outcome="error",
                payload={"reason": reason},
            )
            return

        try:
            changed = diff_files(diff)
        except DiffUnparseable as exc:
            reason = f"diff_unparseable: {exc}"
            await client.fail(vertex_id, reason=reason)
            await client.audit(
                action="fail",
                target=f"vertex:{vertex_id}",
                outcome="error",
                payload={"reason": reason},
            )
            return

        violations = filter_violations(changed, request.paths_allow)
        if violations:
            reason = f"paths_violation: {violations}"
            await client.fail(vertex_id, reason=reason)
            await client.audit(
                action="fail",
                target=f"vertex:{vertex_id}",
                outcome="error",
                payload={"reason": reason},
            )
            return

        try:
            spec = parse_done_when(request.done_when)
        except DoneWhenUnparseable:
            reason = "unparseable_done_when"
            await client.fail(vertex_id, reason=reason)
            await client.audit(
                action="fail",
                target=f"vertex:{vertex_id}",
                outcome="error",
                payload={"reason": reason},
            )
            return

        verdict = validate_done_when(spec, cwd=repo_dir)
        session.session_log.append(
            "done_when_check", {"verdict": verdict.model_dump()}
        )
        if not verdict.passed:
            await client.fail(vertex_id, reason=verdict.reason or "done_when_failed")
            await client.audit(
                action="fail",
                target=f"vertex:{vertex_id}",
                outcome="error",
                payload={"reason": verdict.reason},
            )
            return

        branch = request.work_branch
        await asyncio.to_thread(
            push_head,
            repo_dir,
            branch,
            message=f"agent: {request.name}",
            token=_git_token(settings.git_token),
        )

        result = _build_result(session, changed, verdict, branch)
        session.session_log.append("result", result)
        await client.complete(vertex_id, result)
        await client.audit(
            action="complete",
            target=f"vertex:{vertex_id}",
            outcome="ok",
            payload=result,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            await client.fail(vertex_id, reason=reason)
        await client.audit(
            action="fail",
            target=f"vertex:{vertex_id}",
            outcome="error",
            payload={"reason": reason},
        )
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
        if session is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(session.stop())


async def worker_loop(
    settings: WorkerSettings,
    executor: Executor,
    *,
    http: httpx.AsyncClient | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Claim and execute vertices for ``settings.run_id`` until ``stop`` is set."""
    stop_event = stop or asyncio.Event()
    own_http = http is None
    http = http or httpx.AsyncClient(base_url=settings.api_url)
    client = ControlPlaneClient(http, settings)
    blacklisted: set[int] = set()

    try:
        while not stop_event.is_set():
            try:
                vertex = await client.claim()
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                await client.audit(
                    action="claim_error",
                    target=f"run:{settings.run_id}",
                    outcome="error",
                    payload={"reason": reason},
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=settings.poll_interval_seconds,
                    )
                continue

            if vertex is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=settings.poll_interval_seconds,
                    )
                continue

            vertex_id: int = vertex["id"]
            attempts = int(vertex.get("attempts", 0))
            if attempts >= 3 or vertex_id in blacklisted:
                if vertex_id not in blacklisted:
                    await client.fail(
                        vertex_id, reason="max_attempts_exceeded"
                    )
                    await client.audit(
                        action="fail",
                        target=f"vertex:{vertex_id}",
                        outcome="error",
                        payload={"reason": "max_attempts_exceeded"},
                    )
                    blacklisted.add(vertex_id)
                continue

            await _process_vertex(settings, executor, client, vertex, stop_event)
    finally:
        if own_http:
            await http.aclose()


def main() -> None:
    """Entry point for ``python -m agent_runtime.worker``."""
    settings = WorkerSettings()
    if settings.litellm_api_key:
        os.environ["LITELLM_API_KEY"] = settings.litellm_api_key

    run_ws = _run_workspace(settings)
    repo_dir = ensure_repo(
        run_ws,
        settings.repo_url,
        settings.base_branch,
        token=_git_token(settings.git_token),
    )

    executor = OpencodeExecutor(
        base_url=settings.opencode_base_url,
        workspace=repo_dir,
        evidence_root=run_ws,
        model=settings.model,
        llm_endpoint=settings.litellm_base_url,
        port=settings.opencode_port,
    )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(worker_loop(settings, executor))


if __name__ == "__main__":
    main()
