"""OpenCode harness adapter: docker serve launcher + Executor implementation.

The adapter owns a single :class:`SessionLog` per run, pumps the global SSE stream
into it, detects terminal events, and finalizes the run manifest on completion.
See the verified API contract in
``docs/superpowers/research/2026-11-09-m4a-opencode-serve-spike.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
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
from agent_runtime.manifest import build_manifest, write_manifest
from agent_runtime.opencode_client import OpencodeClient
from agent_runtime.pathguard import diff_files
from agent_runtime.session_log import SessionLog
from agent_runtime.settings import OPENCODE_IMAGE, OPENCODE_PORT

_DockerRunner = Callable[
    [list[str], str | None, dict[str, str] | None], subprocess.CompletedProcess[str]
]


class DockerServe:
    """Build a git-enabled derived image and run ``opencode serve`` in Docker.

    The official pinned image does not contain ``git``, so the launcher builds a
    tiny derived image (``opencode-git:<tag>``) once and uses it for the session.
    The host port is mapped on loopback only to the fixed container port ``4096``.

    ``runner`` is the single command-execution seam: tests inject a callable that
    returns a :class:`subprocess.CompletedProcess`; the default uses the real
    ``subprocess`` runner. The seam accepts an ``input`` string for ``docker build -``
    and an environment mapping so ``docker run -e KEY`` can forward secrets without
    placing their values in argv.
    """

    def __init__(
        self,
        *,
        image: str,
        repo_dir: Path,
        port: int,
        env: dict[str, str],
        runner: _DockerRunner | None = None,
    ) -> None:
        self._image = image
        self._repo_dir = repo_dir
        self._port = port
        self._env = env
        self._runner = runner if runner is not None else _subprocess_runner
        self._container_id: str | None = None
        self._derived_image = _derived_image_tag(image)
        self._auth: tuple[str, str] | None = None

    @property
    def auth(self) -> tuple[str, str] | None:
        """HTTP Basic credentials scraped from the server logs, if present."""
        return self._auth

    def start(self) -> str:
        """Start the container and return a credential-free client ``base_url``."""
        build_result = self._runner(
            self._build_argv(),
            _dockerfile(self._image),
            None,
        )
        if build_result.returncode != 0:
            raise RuntimeError(
                f"docker build failed for {self._derived_image}: {build_result.stderr}"
            )

        run_result = self._runner(self._run_argv(), None, {**os.environ, **self._env})
        if run_result.returncode != 0:
            raise RuntimeError(f"docker run failed: {run_result.stderr}")

        self._container_id = run_result.stdout.strip()
        password = self._scrape_password()
        self._auth = ("opencode", password) if password else None
        return f"http://127.0.0.1:{self._port}"

    def stop(self) -> None:
        """Stop the running container, if any."""
        container_id = self._container_id
        if container_id is None:
            return
        self._runner(["docker", "stop", container_id], None, None)
        self._container_id = None

    def _build_argv(self) -> list[str]:
        return ["docker", "build", "-t", self._derived_image, "-"]

    def _run_argv(self) -> list[str]:
        env_args: list[str] = []
        for key in self._env:
            env_args.extend(["-e", key])
        return [
            "docker",
            "run",
            "-d",
            "--rm",
            "-w",
            "/workspace",
            f"-v{self._repo_dir}:/workspace",
            f"-p127.0.0.1:{self._port}:{OPENCODE_PORT}",
            *env_args,
            self._derived_image,
            "serve",
            "--hostname",
            "0.0.0.0",
            "--port",
            str(OPENCODE_PORT),
            "--print-logs",
        ]

    def _scrape_password(self) -> str:
        if self._container_id is None:
            return ""
        for _ in range(60):
            result = self._runner(["docker", "logs", self._container_id], None, None)
            output = (result.stdout or "") + (result.stderr or "")
            password = _password_from_logs(output)
            if password:
                return password
            time.sleep(0.5)
        return ""


class OpencodeExecutor:
    """Executor that drives an ``opencode serve`` harness, local or remote."""

    def __init__(
        self,
        *,
        base_url: str | None,
        workspace: Path,
        image: str = OPENCODE_IMAGE,
        model: str,
        llm_endpoint: str,
        port: int = OPENCODE_PORT,
        transport: httpx.AsyncBaseTransport | None = None,
        docker_runner: _DockerRunner | None = None,
        evidence_root: Path | None = None,
    ) -> None:
        self._base_url = base_url
        self._workspace = workspace
        self._image = image
        self._model = model
        self._llm_endpoint = llm_endpoint
        self._port = port
        self._transport = transport
        self._docker_runner = docker_runner
        self._evidence_root = evidence_root or workspace
        self._serve: DockerServe | None = None

    def build_prompt(self, request: ExecutionRequest) -> str:
        """Render a human-readable prompt for the harness from the vertex."""
        allowed = ", ".join(request.paths_allow) if request.paths_allow else "(none)"
        return (
            f"Vertex: {request.name}\n"
            f"Repository: {request.repo_url} (base: {request.base_branch}, "
            f"work: {request.work_branch})\n"
            f"Allowed paths: {allowed}\n"
            f"Done when: {request.done_when}\n\n"
            f"{request.prompt}"
        )

    async def start(self, request: ExecutionRequest) -> ExecutionSession:
        """Begin execution: log, manifest, harness session, prompt."""
        evidence_dir = _evidence_dir(self._evidence_root, request)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        session_log_path = evidence_dir / "session_log.ndjson"
        manifest_path = evidence_dir / "manifest.json"

        session_log = SessionLog(session_log_path)
        prompt_text = self.build_prompt(request)
        session_log.append(
            "prompt",
            {"text": prompt_text, "name": request.name, "vertex_id": request.vertex_id},
        )

        ts_start = datetime.now(UTC)
        manifest = build_manifest(
            request=request,
            image=self._image,
            model=self._model,
            llm_endpoint=self._llm_endpoint,
            ts_start=ts_start,
            ts_end=None,
            session_log_path=session_log_path,
        )
        write_manifest(manifest_path, manifest)

        base_url = self._base_url
        auth: tuple[str, str] | None = None
        if base_url is None:
            self._write_config(self._workspace)
            self._serve = DockerServe(
                image=self._image,
                repo_dir=self._workspace,
                port=self._port,
                env={"LITELLM_API_KEY": os.environ.get("LITELLM_API_KEY", "")},
                runner=self._docker_runner,
            )
            base_url = await asyncio.to_thread(self._serve.start)
            auth = self._serve.auth

        http = httpx.AsyncClient(
            base_url=base_url,
            auth=auth,
            transport=self._transport,
            timeout=httpx.Timeout(600.0),
        )
        client = OpencodeClient(http)
        try:
            session_id = await client.create_session()
            stream = await client.events(session_id)
            await client.send_prompt(session_id, prompt_text)
        except Exception:
            await http.aclose()
            if self._serve is not None:
                await asyncio.to_thread(self._serve.stop)
            raise

        return OpencodeSession(
            client=client,
            session_id=session_id,
            session_log=session_log,
            manifest_path=manifest_path,
            http=http,
            stream=stream,
            on_stop=self._stop_serve,
        )

    async def _stop_serve(self) -> None:
        if self._serve is not None:
            await asyncio.to_thread(self._serve.stop)

    def _write_config(self, workspace: Path) -> None:
        provider_id, model_id = _split_model(self._model)
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": f"{provider_id}/{model_id}",
            "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
            "providers": {
                provider_id: {
                    "name": f"{provider_id} provider",
                    "env": ["LITELLM_API_KEY"],
                    "package": "@opencode/ai/providers/openai-compatible",
                    "settings": {
                        "baseURL": self._llm_endpoint,
                        "apiKey": "{env:LITELLM_API_KEY}",
                    },
                    "models": {
                        model_id: {
                            "name": model_id,
                            "modelID": model_id,
                        }
                    },
                }
            },
        }
        (workspace / "opencode.jsonc").write_text(
            f"{json.dumps(config, indent=2, ensure_ascii=False)}\n",
            encoding="utf-8",
        )


class OpencodeSession:
    """A running harness session: SSE pump, status, diff, manifest finalize."""

    _SUCCEEDED = "session.execution.succeeded"
    _FAILED = "session.execution.failed"

    def __init__(
        self,
        *,
        client: OpencodeClient,
        session_id: str,
        session_log: SessionLog,
        manifest_path: Path,
        http: httpx.AsyncClient,
        stream: Any,
        on_stop: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._session_log = session_log
        self._manifest_path = manifest_path
        self._http = http
        self._stream = stream
        self._on_stop = on_stop
        self._status = ExecutionStatus.RUNNING
        self._result: ExecutionResult | None = None
        self._diff = ""
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._done_event = asyncio.Event()
        self._finalized = False
        self._released = False
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            async for event in self._stream:
                self._session_log.append("harness_event", event)
                await self._queue.put(event)
                if self._status != ExecutionStatus.RUNNING:
                    break
                if event.get("type") == self._SUCCEEDED:
                    self._status = ExecutionStatus.DONE
                    break
                if event.get("type") == self._FAILED:
                    self._status = ExecutionStatus.FAILED
                    break
        except asyncio.CancelledError:
            if self._status == ExecutionStatus.RUNNING:
                self._status = ExecutionStatus.STOPPED
            raise
        except Exception as exc:
            if self._status == ExecutionStatus.RUNNING:
                self._status = ExecutionStatus.FAILED
            self._session_log.append(
                "harness_error",
                {"message": str(exc), "type": type(exc).__name__},
            )
        finally:
            if self._stream is not None:
                await self._stream.aclose()
            if self._status == ExecutionStatus.RUNNING:
                self._status = ExecutionStatus.FAILED
            await self._queue.put(None)
            self._done_event.set()

    async def events(self) -> AsyncIterator[ExecutionEvent]:
        """Yield events belonging to this session in stream order."""
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield self._to_execution_event(event)

    def _to_execution_event(self, event: dict[str, Any]) -> ExecutionEvent:
        ts_raw = event.get("created")
        ts = (
            datetime.fromtimestamp(ts_raw / 1000, tz=UTC)
            if isinstance(ts_raw, int)
            else datetime.now(UTC)
        )
        return ExecutionEvent(
            ts=ts,
            type=str(event.get("type", "")),
            payload=event.get("data", {}) or {},
        )

    async def wait_done(self) -> ExecutionStatus:
        await self._done_event.wait()
        if not self._finalized:
            if self._status == ExecutionStatus.DONE:
                await self._finalize_done()
            else:
                await self._finalize_non_done()
        return self._status

    async def diff(self) -> str:
        if self._diff:
            return self._diff
        return await self._client.diff(self._session_id)

    async def status(self) -> ExecutionStatus:
        return self._status

    async def stop(self) -> None:
        if self._status == ExecutionStatus.RUNNING:
            self._status = ExecutionStatus.STOPPED
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
        if self._released:
            return
        self._released = True
        if self._stream is not None:
            await self._stream.aclose()
        if not self._finalized:
            await self._finalize_non_done()
        await self._http.aclose()
        if self._on_stop is not None:
            await self._on_stop()
        self._done_event.set()

    @property
    def result(self) -> ExecutionResult | None:
        return self._result

    @property
    def session_log(self) -> SessionLog:
        """The single :class:`SessionLog` instance for this run."""
        return self._session_log

    async def _finalize_done(self) -> None:
        error_message: str | None = None
        try:
            self._diff = await self._client.diff(self._session_id)
            tokens = await self._fetch_tokens()
            diff_files_list = diff_files(self._diff)
            diff_stat = _diff_stat(self._diff)
            self._result = ExecutionResult(
                diff=self._diff,
                diff_files=diff_files_list,
                session_log_path=self._session_log.path,
                manifest_path=self._manifest_path,
                tokens_used=tokens,
            )
        except Exception as exc:
            error_message = str(exc)
            self._status = ExecutionStatus.FAILED
            self._session_log.append(
                "harness_error",
                {"message": error_message, "type": type(exc).__name__},
            )
            self._result = None
            tokens = await self._fetch_tokens()
            diff_stat = None

        self._update_manifest(
            diff_stat=diff_stat,
            tokens_used=tokens,
            error=error_message,
        )
        self._finalized = True

    async def _finalize_non_done(self) -> None:
        tokens = await self._fetch_tokens()
        self._update_manifest(diff_stat=None, tokens_used=tokens, error=None)
        self._finalized = True

    async def _fetch_tokens(self) -> int | None:
        try:
            data = await self._client.get_session(self._session_id)
        except Exception:
            return None
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return None
        return _sum_tokens(tokens)

    def _update_manifest(
        self,
        *,
        diff_stat: str | None,
        tokens_used: int | None,
        error: str | None,
    ) -> None:
        if not self._manifest_path.exists():
            return
        manifest: dict[str, Any] = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        manifest["ts_end"] = datetime.now(UTC).isoformat()
        manifest["diff_stat"] = diff_stat
        manifest["tokens_used"] = tokens_used
        if error is not None:
            manifest["error"] = error
        write_manifest(self._manifest_path, manifest)


def _evidence_dir(workspace: Path, request: ExecutionRequest) -> Path:
    return (
        workspace
        / ".agent_runtime"
        / "runs"
        / f"run-{request.run_id}"
        / f"vertex-{request.vertex_id}"
    )


def _derived_image_tag(image: str) -> str:
    tag = "latest"
    if ":" in image:
        candidate = image.rsplit(":", 1)[1]
        if "/" not in candidate:
            tag = candidate
    return f"opencode-git:{tag}"


def _subprocess_runner(
    argv: list[str],
    input: str | None = None,  # noqa: A002
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        input=input,
        env=env,
    )


def _password_from_logs(logs: str) -> str:
    for line in logs.splitlines():
        if "server password" in line:
            return line.split("server password", 1)[1].strip()
    return ""


def _dockerfile(base_image: str) -> str:
    # M4b: root here may create root-owned files in the host bind mount.
    return (
        f"FROM {base_image}\n"
        "USER root\n"
        "RUN if command -v apt-get >/dev/null 2>&1; then "
        "apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*; "
        "elif command -v apk >/dev/null 2>&1; then "
        "apk add --no-cache git; "
        "else echo 'No supported package manager found' && exit 1; fi\n"
    )


def _split_model(model: str) -> tuple[str, str]:
    if "/" in model:
        provider_id, model_id = model.split("/", 1)
        return provider_id, model_id
    return "litellm", model


def _sum_tokens(tokens: dict[str, Any]) -> int | None:
    try:
        total = 0
        for key in ("input", "output", "reasoning"):
            value = tokens.get(key)
            if isinstance(value, int):
                total += value
        return total
    except Exception:
        return None


def _diff_stat(diff: str) -> str | None:
    files = diff_files(diff)
    if not files:
        return "0 files changed"
    additions = sum(
        1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")
    )
    file_word = "file" if len(files) == 1 else "files"
    ins_word = "insertion" if additions == 1 else "insertions"
    del_word = "deletion" if deletions == 1 else "deletions"
    return f"{len(files)} {file_word} changed, {additions} {ins_word}(+), {deletions} {del_word}(-)"
