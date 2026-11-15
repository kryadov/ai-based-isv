"""Run-scoped vertex worker: claim vertices from agent_api, execute them, report back.

The worker loop is the integration seam between the control plane (M3), the
executor adapter (Task 7), gitops (Task 8) and validation (Task 4). It is
intentionally plain asyncio + httpx: no FastAPI/SQLAlchemy inside the runtime
package.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast

import httpx

from agent_runtime.done_when import (
    DoneWhenUnparseable,
    DoneWhenVerdict,
    parse_done_when,
    validate_done_when,
)
from agent_runtime.executor import ExecutionRequest, ExecutionSession, ExecutionStatus, Executor
from agent_runtime.gitops import apply_diff, clone_local, ensure_repo, push_head
from agent_runtime.kube_sandbox import (
    KubeSandboxExecutorFactory,
    reconcile_orphans,
)
from agent_runtime.kube_sandbox import (
    kubectl_runner as _kubectl_runner,
)
from agent_runtime.manifest import write_manifest
from agent_runtime.memory import (
    MemoryClient,
    extract_lessons,
    memory_prompt_section,
    project_of,
)
from agent_runtime.merge_policy import touches_protected
from agent_runtime.opencode_executor import OpencodeExecutor
from agent_runtime.pathguard import DiffUnparseable, diff_files, filter_violations
from agent_runtime.settings import WorkerSettings
from agent_runtime.tracker import TrackerClient
from agent_runtime.vault_broker import read_gitea_token
from agent_runtime.worker_deploy import DeployRunner, default_deploy_runner
from agent_runtime.worker_gates import (
    GateContext,
    GateRunner,
    default_gate_runner,
    fix_attempt,
    read_policy,
    reviewer_base,
)

logger = logging.getLogger(__name__)

# Attempt budget per vertex; a vertex claimed at or past it is failed, not run.
MAX_ATTEMPTS = 3


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

    async def fail(
        self, vertex_id: int, reason: str | None = None, *, terminal: bool = False
    ) -> None:
        payload: dict[str, Any] = {"agent": self._settings.agent}
        if reason is not None:
            payload["reason"] = reason
        if terminal:
            payload["terminal"] = True
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

    async def note(self, text: str) -> None:
        """A pipeline stage note for the tracker; no tracker means nothing to do."""
        return None

    async def escalate(self, subject: str, context: dict[str, Any]) -> None:
        """File a HITL ``escalation`` (a dispute the agent could not settle)."""
        with contextlib.suppress(Exception):
            await self._http.post(
                "/hitl", json={"kind": "escalation", "subject": subject, "context": context}
            )


class TrackedControlPlaneClient(ControlPlaneClient):
    """ControlPlaneClient that mirrors vertex transitions into the tracker.

    claim -> ``in-progress``, complete -> ``done`` (branch + verdict + evidence),
    fail -> ``blocked`` (reason). The tracker is observability, not acceptance:
    a tracker failure is logged and audited (``tracker_error``) and NEVER fails
    the vertex.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: WorkerSettings,
        tracker: TrackerClient,
    ) -> None:
        super().__init__(http, settings)
        self._tracker = tracker
        self._task_ref = settings.task_ref

    async def claim(self) -> dict[str, Any] | None:
        vertex = await super().claim()
        # An exhausted vertex is re-claimed only to be failed (agent_api has no
        # terminal failed state): never report it in progress again.
        if vertex is not None and int(vertex.get("attempts", 0)) < MAX_ATTEMPTS:
            note = (
                f"run {self._settings.run_id}: vertex {vertex.get('id')} "
                f"({vertex.get('name', '')}) claimed by {self._settings.agent}"
            )
            await self._track("in-progress", note, vertex.get("id"))
        return vertex

    async def complete(self, vertex_id: int, result: dict[str, Any]) -> None:
        await super().complete(vertex_id, result)
        verdict = result.get("verdict") or {}
        note = (
            f"run {self._settings.run_id}: vertex {vertex_id} done\n"
            f"branch: {result.get('branch', '')}\n"
            f"verdict: {'PASS' if verdict.get('passed') else 'FAIL'}\n"
            f"evidence: {result.get('manifest_path', '')}"
        )
        if result.get("pr_url"):
            note += f"\nPR: {result['pr_url']}"
        await self._track("done", note, vertex_id)

    async def fail(
        self, vertex_id: int, reason: str | None = None, *, terminal: bool = False
    ) -> None:
        await super().fail(vertex_id, reason=reason, terminal=terminal)
        note = f"run {self._settings.run_id}: vertex {vertex_id} failed: {reason or 'unknown'}"
        await self._track("blocked", note, vertex_id)

    async def note(self, text: str) -> None:
        # Gate stage comments (PR opened, CI, review, fix round, merged).
        try:
            await self._tracker.add_comment(self._task_ref, f"[platform] {text}")
        except Exception as exc:
            logger.warning("tracker comment failed task_ref=%s: %s", self._task_ref, exc)

    async def _track(self, status: str, note: str, vertex_id: Any) -> None:
        try:
            await self._tracker.set_status(self._task_ref, status, note)
        except Exception as exc:
            logger.warning(
                "tracker update failed task_ref=%s status=%s: %s", self._task_ref, status, exc
            )
            await self.audit(
                action="tracker_error",
                target=f"task:{self._task_ref}",
                outcome="error",
                payload={"status": status, "vertex_id": vertex_id, "error": str(exc)},
            )


def _git_token(raw: str) -> str | None:
    return raw if raw else None


def prepare_settings(settings: WorkerSettings, kube_runner: Any | None = None) -> WorkerSettings:
    """Return settings whose ``git_token`` is the resolved credential.

    On the k3s backend the token may come from OpenBao only; every host-side
    git call in the loop (fetch, apply, push) reads ``settings.git_token``, so
    the brokered value must land there once, at startup. Docker is untouched.
    """
    if settings.sandbox_backend != "k3s":
        return settings
    token = resolve_gitea_token(settings, kube_runner)
    return settings.model_copy(update={"git_token": token})


def resolve_gitea_token(settings: WorkerSettings, kube_runner: Any | None = None) -> str:
    """Resolve the Gitea credential: explicit env token first, OpenBao second.

    The brokered token feeds BOTH the host-side git operations and the sandbox
    pod, so a Vault-only worker can clone and push private repositories.
    """
    explicit = _git_token(settings.git_token)
    if explicit is not None:
        return explicit
    root_token = settings.vault_root_token
    if not root_token:
        path = settings.vault_root_token_path
        if not path.exists():
            raise ValueError(
                "k3s backend needs a Gitea token: set AGENT_RUNTIME_GIT_TOKEN "
                f"or provide the vault root token file at {path}"
            )
        root_token = path.read_text(encoding="utf-8").strip()
    kubectl: Any = kube_runner if kube_runner is not None else _kubectl_runner
    return read_gitea_token(
        kubectl,
        root_token=root_token,
        namespace=settings.openbao_namespace,
        pod_label=settings.openbao_pod_label,
    )


def _mcp_servers(settings: WorkerSettings) -> dict[str, str]:
    """Harness MCP registrations: server name -> URL, configured ones only."""
    configured = {
        "tracker": settings.mcp_task_source_url,
        "llm": settings.mcp_llm_url,
        "observability": settings.mcp_observability_url,
    }
    return {name: url for name, url in configured.items() if url}


def build_executor(
    settings: WorkerSettings,
    *,
    repo_dir: Path,
    gitea_token: str,
    kube_runner: Any | None = None,
) -> Executor:
    """Construct the backend executor: docker spawn (M4a) or k3s sandbox (M4b).

    ``gitea_token`` is the RESOLVED credential (see resolve_gitea_token); the
    k3s sandbox requires it, the docker path ignores it (it uses env creds).
    """
    if settings.sandbox_backend == "k3s":
        return KubeSandboxExecutorFactory(
            gitea_token=gitea_token,
            litellm_api_key=settings.litellm_api_key,
            litellm_base_url=settings.litellm_base_url,
            model=settings.model,
            evidence_root=_run_workspace(settings),
            namespace=settings.k8s_namespace,
            nodeport_range=(settings.nodeport_range_low, settings.nodeport_range_high),
            kube_runner=kube_runner,
            mcp_servers=_mcp_servers(settings),
        )
    sandbox_dir = _reset_sandbox(repo_dir, _sandbox_dir(settings))
    return OpencodeExecutor(
        base_url=settings.opencode_base_url,
        workspace=sandbox_dir,
        evidence_root=_run_workspace(settings),
        model=settings.model,
        llm_endpoint=settings.litellm_base_url,
        port=settings.opencode_port,
    )


def startup_executor(
    settings: WorkerSettings,
    *,
    repo_dir: Path | None = None,
    kube_runner: Any | None = None,
) -> Executor:
    """Worker-startup executor construction: token resolution + orphan sweep.

    k3s only: BEFORE the first claim, sweep sandbox objects orphaned by dead
    workers (they hold credentials and NodePorts). v0 is single-worker per
    run — the age-based sweep is safe under that contract (see STATE.md).
    """
    if settings.sandbox_backend != "k3s":
        if repo_dir is None:
            raise ValueError("the docker backend needs repo_dir")
        return build_executor(settings, repo_dir=repo_dir, gitea_token="", kube_runner=kube_runner)
    kubectl: Any = kube_runner if kube_runner is not None else _kubectl_runner
    reconcile_orphans(
        kubectl,
        namespace=settings.k8s_namespace,
        max_age_seconds=float(settings.lease_ttl_seconds) * 2,
    )
    return build_executor(
        settings,
        repo_dir=repo_dir or _run_workspace(settings),
        gitea_token=resolve_gitea_token(settings, kube_runner),
        kube_runner=kube_runner,
    )


def _run_workspace(settings: WorkerSettings) -> Path:
    return settings.workspace_dir / f"run-{settings.run_id}"


def _sandbox_dir(settings: WorkerSettings) -> Path:
    return _run_workspace(settings) / "sandbox"


def _reset_sandbox(repo_dir: Path, sandbox_dir: Path) -> Path:
    """Replace ``sandbox_dir`` with a fresh local clone of ``repo_dir``."""
    if sandbox_dir.exists():
        shutil.rmtree(sandbox_dir)
    return clone_local(repo_dir, sandbox_dir)


class MemoryPort(Protocol):
    """Project episodic memory (M7): fail-open slice/add (see agent_runtime.memory)."""

    async def slice(
        self, project: str, paths: list[str], text: str, limit: int = 5
    ) -> list[dict[str, Any]]: ...

    async def add(
        self,
        project: str,
        kind: str,
        title: str,
        body: str,
        paths: list[str],
        *,
        run_id: int | None = None,
        vertex_id: int | None = None,
        tags: list[str] | None = None,
    ) -> int | None: ...


Extractor = Callable[[str, list[str], str], Awaitable[list[dict[str, str]]]]


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
        "the worker will collect them from the session diff. Do not push or commit. "
        "Do not run commands or tests: the sandbox cannot execute them, and "
        "the platform runs the done-when checks after you finish. "
        "Write the files, then stop."
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
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verdict"] = verdict
    write_manifest(manifest_path, manifest)


async def _sleep_or_stop(stop: asyncio.Event, timeout: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout)


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
            await _sleep_or_stop(stop, interval)
            if stop.is_set():
                return
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
    gate_runner: GateRunner | None = None,
    deploy_runner: DeployRunner | None = None,
    memory: MemoryPort | None = None,
    extractor: Extractor | None = None,
) -> None:
    vertex_id: int = vertex["id"]
    project = project_of(settings.task_ref, settings.repo_url)

    async def remember(kind: str, title: str, body: str, paths: list[str]) -> None:
        if memory is not None:
            await memory.add(
                project, kind, title, body, paths, run_id=settings.run_id, vertex_id=vertex_id
            )

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

        # The sandbox works on a disposable copy; the pristine clone is kept clean
        # for validation and push.
        sandbox_dir = await asyncio.to_thread(_reset_sandbox, repo_dir, _sandbox_dir(settings))

        # Project memory slice (M7): lessons of earlier tasks on the same project,
        # ranked by path overlap and task words; fail-open (empty on any error).
        paths_allow = [str(p) for p in vertex.get("paths_allow", [])]
        memory_slice = (
            await memory.slice(project, paths_allow, str(vertex.get("name", "")))
            if memory is not None
            else []
        )
        prompt = build_vertex_prompt(vertex)
        section = memory_prompt_section(memory_slice)
        if section:
            prompt = f"{prompt}\n\n{section}"
        request = ExecutionRequest(
            run_id=settings.run_id,
            vertex_id=vertex_id,
            name=vertex["name"],
            prompt=prompt,
            paths_allow=list(vertex.get("paths_allow", [])),
            done_when=vertex.get("done_when", ""),
            repo_url=settings.repo_url,
            base_branch=settings.base_branch,
            work_branch=f"agent/vertex-{vertex_id}",
        )

        # The real executor is constructed with workspace=<sandbox_dir>; tests with
        # a FakeExecutor ignore the filesystem, so the sandbox copy is still created
        # and is validated by integration tests.
        _ = sandbox_dir
        session = await executor.start(request)
        if memory_slice:
            session.session_log.append(
                "memory_slice", {"episodes": [e.get("id") for e in memory_slice]}
            )

        wait_task = asyncio.create_task(session.wait_done())
        lease_wait = asyncio.create_task(lease_lost.wait())
        done, pending = await asyncio.wait(
            {wait_task, lease_wait},
            timeout=settings.session_timeout_seconds,
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

        if not done:
            # A harness that never finishes (live m4c: a model looping in its
            # reasoning) must not burn the whole run budget: fail this attempt
            # so the control plane can requeue it; finally stops the session.
            reason = "session_timeout"
            await client.fail(vertex_id, reason=reason)
            await client.audit(
                action="fail",
                target=f"vertex:{vertex_id}",
                outcome="error",
                payload={"reason": reason, "timeout_seconds": settings.session_timeout_seconds},
            )
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

        # Fail fast on malformed diffs before touching the pristine clone.
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

        # Gate paths (.gitea/**, .platform/**) are never an implementer's to change.
        violations = filter_violations(changed, request.paths_allow) + touches_protected(changed)
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

        # Apply the agent's diff onto the pristine clone, then validate and push.
        try:
            await asyncio.to_thread(
                apply_diff,
                repo_dir,
                diff,
                token=_git_token(settings.git_token),
            )
        except Exception as exc:
            reason = (
                "empty_diff" if "empty diff" in str(exc).lower() else f"apply_diff_failed: {exc}"
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

        verdict = await asyncio.to_thread(validate_done_when, spec, cwd=repo_dir)
        session.session_log.append("done_when_check", {"verdict": verdict.model_dump()})
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

        if settings.gates_enabled:
            # M5: the vertex is done only when its PR is merged. The implementer
            # sandbox is done: tear it down first — a fix attempt starts a sandbox
            # with the same name (agent-<run>-<vertex>).
            with contextlib.suppress(Exception):
                await session.stop()
            token = _git_token(settings.git_token)
            runner = gate_runner or default_gate_runner(settings, token or "")

            async def fix(req: Any) -> Any:
                return await fix_attempt(
                    settings=settings,
                    executor=executor,
                    vertex=vertex,
                    req=req,
                    repo_dir=repo_dir,
                    branch=branch,
                    token=token,
                )

            ctx = GateContext(
                vertex=vertex,
                branch=branch,
                title=f"agent: {request.name} (vertex {vertex_id})",
                body=(
                    f"Run {settings.run_id}, vertex {vertex_id}: {request.name}\n\n"
                    f"verdict: {'PASS' if verdict.passed else 'FAIL'}\n"
                    f"files: {', '.join(changed)}\n"
                    f"evidence: {result.get('manifest_path', '')}"
                ),
                goal=build_vertex_prompt(vertex),
                policy_text=await asyncio.to_thread(read_policy, repo_dir, settings.base_branch),
                fix=fix,
                notify=client.note,
            )
            outcome = await runner(ctx, stop)
            session.session_log.append("gates", dataclasses.asdict(outcome))
            if outcome.status == "stopped":
                return  # the lease expires; the PR stays open for the next attempt
            if outcome.status != "merged":
                reason = f"gate:{outcome.reason}"
                if outcome.reason == "fix_rounds_exhausted":
                    await client.escalate(
                        outcome.pr_url,
                        {"pr_url": outcome.pr_url, "vertex_id": vertex_id, "reason": reason},
                    )
                # A human rejection is final: retrying would ask the human again.
                terminal = outcome.reason == "hitl_rejected"
                await client.fail(vertex_id, reason=reason, terminal=terminal)
                if terminal:
                    await remember(
                        "failure",
                        f"{vertex.get('name', '')}: {reason}",
                        f"PR {outcome.pr_url} was rejected by the human reviewer.",
                        paths_allow,
                    )
                await client.audit(
                    action="fail",
                    target=f"vertex:{vertex_id}",
                    outcome="error",
                    payload={"reason": reason, "pr_url": outcome.pr_url},
                )
                return
            result["pr_url"] = outcome.pr_url
            result["auto_merged"] = outcome.auto_merged
            result["review_summary"] = outcome.review_summary

            if settings.deploy_enabled:
                deployer = deploy_runner or default_deploy_runner(settings, token)
                deployed = await deployer(changed)
                session.session_log.append("deploy", deployed.model_dump())
                result["deploy_verdict"] = deployed.model_dump(exclude={"postmortem"})
                await client.note(f"deployed {deployed.image} to prod: {deployed.status}")
                if deployed.status == "succeeded":
                    await remember(
                        "deploy",
                        f"deployed {deployed.image}: succeeded (revision {deployed.revision})",
                        f"Canary passed analysis; previous {deployed.previous_image}.",
                        changed,
                    )
                elif deployed.postmortem:
                    await remember(
                        "postmortem",
                        f"canary {deployed.image} rolled back",
                        deployed.postmortem,
                        changed,
                    )
                if deployed.postmortem:
                    manifest = result.get("manifest_path")
                    evidence = (
                        Path(manifest).parent
                        if manifest
                        else Path(settings.workspace_dir) / f"run-{settings.run_id}"
                    )
                    evidence.mkdir(parents=True, exist_ok=True)
                    postmortem = evidence / "postmortem.md"
                    postmortem.write_text(deployed.postmortem, encoding="utf-8")
                    result["postmortem_path"] = str(postmortem)
                if deployed.status != "succeeded":
                    # A rollback (or a stuck/rejected rollout) is a verdict, not a
                    # transient error: re-running the agent would not change it.
                    reason = f"deploy:{deployed.status}"
                    await client.fail(vertex_id, reason=reason, terminal=True)
                    if not deployed.postmortem:
                        await remember(
                            "failure", f"{vertex.get('name', '')}: {reason}", reason, changed
                        )
                    await client.audit(
                        action="fail",
                        target=f"vertex:{vertex_id}",
                        outcome="error",
                        payload={"reason": reason, **result},
                    )
                    return

        if extractor is not None and memory is not None:
            for lesson in await extractor(
                str(vertex.get("name", "")), changed, str(result.get("review_summary", ""))
            ):
                await remember("lesson", lesson["title"], lesson["body"], changed)
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
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await heartbeat_task
        if session is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(session.stop())


async def worker_loop(
    settings: WorkerSettings,
    executor: Executor,
    *,
    http: httpx.AsyncClient | None = None,
    stop: asyncio.Event | None = None,
    tracker: TrackerClient | None = None,
    gate_runner: GateRunner | None = None,
    deploy_runner: DeployRunner | None = None,
    memory: MemoryPort | None = None,
    extractor: Extractor | None = None,
) -> None:
    """Claim and execute vertices for ``settings.run_id`` until ``stop`` is set.

    With ``settings.task_ref`` set, vertex transitions are mirrored into the
    tracker (``tracker`` injected, or built from ``settings.task_source_url``);
    an empty ``task_ref`` disables the loop.
    """
    stop_event = stop or asyncio.Event()
    own_http = http is None
    http = http or httpx.AsyncClient(base_url=settings.api_url)
    tracker_http: httpx.AsyncClient | None = None
    if settings.task_ref and tracker is None and settings.task_source_url:
        tracker_http = httpx.AsyncClient(timeout=30.0)
        tracker = TrackerClient(tracker_http, settings.task_source_url)
    client: ControlPlaneClient = (
        TrackedControlPlaneClient(http, settings, tracker)
        if settings.task_ref and tracker is not None
        else ControlPlaneClient(http, settings)
    )
    blacklisted: set[int] = set()
    llm_http: httpx.AsyncClient | None = None
    if settings.memory_enabled and memory is None:
        memory = MemoryClient(http, settings.api_url)
    if settings.memory_enabled and extractor is None:
        llm_http = httpx.AsyncClient(timeout=120.0)
        extractor = _default_extractor(settings, llm_http)

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
                await _sleep_or_stop(stop_event, settings.poll_interval_seconds)
                continue

            if vertex is None:
                await _sleep_or_stop(stop_event, settings.poll_interval_seconds)
                continue

            vertex_id: int = vertex["id"]
            attempts = int(vertex.get("attempts", 0))
            if attempts >= MAX_ATTEMPTS or vertex_id in blacklisted:
                if vertex_id not in blacklisted:
                    await client.fail(vertex_id, reason="max_attempts_exceeded")
                    await client.audit(
                        action="fail",
                        target=f"vertex:{vertex_id}",
                        outcome="error",
                        payload={"reason": "max_attempts_exceeded"},
                    )
                    if memory is not None:
                        await memory.add(
                            project_of(settings.task_ref, settings.repo_url),
                            "failure",
                            f"{vertex.get('name', '')}: max_attempts_exceeded",
                            f"The vertex failed {attempts} attempts and was given up.",
                            [str(p) for p in vertex.get("paths_allow", [])],
                            run_id=settings.run_id,
                            vertex_id=vertex_id,
                        )
                    blacklisted.add(vertex_id)
                await _sleep_or_stop(stop_event, settings.poll_interval_seconds)
                continue

            await _process_vertex(
                settings,
                executor,
                client,
                vertex,
                stop_event,
                gate_runner,
                deploy_runner,
                memory,
                extractor,
            )
    finally:
        if own_http:
            await http.aclose()
        if llm_http is not None:
            await llm_http.aclose()
        if tracker_http is not None:
            await tracker_http.aclose()


def _default_extractor(settings: WorkerSettings, http: httpx.AsyncClient) -> Extractor:
    """memory-extractor via the host-reachable LiteLLM light alias (fail-open)."""

    async def extract(task: str, changed: list[str], review: str) -> list[dict[str, str]]:
        return await extract_lessons(
            http,
            base_url=f"{reviewer_base(settings)}/v1",
            api_key=settings.litellm_api_key,
            model=settings.extractor_model,
            task=task,
            changed=changed,
            review_summary=review,
        )

    return extract


def main() -> None:
    """Entry point for ``python -m agent_runtime.worker``."""
    settings = WorkerSettings()
    if settings.litellm_api_key:
        os.environ["LITELLM_API_KEY"] = settings.litellm_api_key

    # The brokered token must exist BEFORE the first host-side git call.
    settings = prepare_settings(settings)
    repo_dir = ensure_repo(
        _run_workspace(settings),
        settings.repo_url,
        settings.base_branch,
        token=_git_token(settings.git_token),
    )
    executor = startup_executor(settings, repo_dir=repo_dir)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(worker_loop(settings, executor))


if __name__ == "__main__":
    main()
