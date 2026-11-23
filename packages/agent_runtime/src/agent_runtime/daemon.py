"""Platform daemon (M11): the standing executor side of the platform.

    python -m agent_runtime.daemon        (run by ``infra/scripts/platform.sh up``)

One long-lived process on the stand host (a toolbox-image container with
``--restart unless-stopped``):

- the **dispatcher** loop (``dispatcher.Dispatcher``) turns ``swarm``-labelled
  issues into runs and spawns run-scoped worker subprocesses;
- the **automation poller** (``agent_runtime.automations poll``) runs as a
  supervised child process: panel requests, cron schedules, card decisions.

Secrets are read from OpenBao at startup (the LiteLLM master key; the
``swarm-agent`` Gitea token for the tracker side) and passed only to child
processes' environments — never on a command line, never logged. Workers
resolve their own Gitea credential from OpenBao, so no git token is set.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_runtime.dispatcher import Dispatcher, Task
from agent_runtime.planner import plan_task
from agent_runtime.project_config import ProjectConfig
from agent_runtime.swarm import JudgeVerdict, _check_main_factory, judge_run, post_plan, worker_env
from agent_runtime.vault_broker import AGENT_TOKEN_PATH, VaultBrokerError, read_gitea_token

LITELLM_MASTER_PATH = "secret/litellm/master"
SERVICE_TOKEN_PATH = "secret/platform/api"
TASK_SOURCE_TOKEN_PATH = "secret/mcp/task-source"  # M12, written by bootstrap.sh mcp
INTAKE_LABEL = "swarm"


class DaemonSettings(BaseSettings):
    """``SWARM_DAEMON_*`` env vars; the defaults are the stand's addresses."""

    model_config = SettingsConfigDict(env_prefix="SWARM_DAEMON_")

    repo_root: Path = Path(".")  # the dev checkout holding infra/.vault-root-token
    workspace_dir: Path = Path(".agent-runtime-workspace/daemon")
    api_url: str = "http://localhost:30400"
    gitea_url: str = "http://localhost:30080"
    org: str = "swarm-lab"
    interval_s: float = 30.0
    max_active_runs: int = 2
    sandbox_cap: int = 11
    model: str = "litellm/qwen3"
    planner_model: str = "qwen3"
    session_timeout_s: int = 600
    shutdown_grace_s: float = 60.0  # M12: workers release their vertex, then are killed
    host_litellm_url: str = "http://localhost:30084/v1"


@dataclass(frozen=True)
class Secrets:
    litellm_key: str = field(default="", repr=False)
    gitea_token: str = field(default="", repr=False)
    api_token: str = field(default="", repr=False)  # the daemon's own identity
    worker_api_token: str = field(default="", repr=False)  # M12: per-identity tokens
    poller_api_token: str = field(default="", repr=False)
    task_source_token: str = field(default="", repr=False)


def build_worker_env(
    settings: DaemonSettings,
    secrets: Secrets,
    *,
    run_id: int,
    repo: str,
    cfg: ProjectConfig,
    base: dict[str, str],
    task_ref: str = "",
) -> dict[str, str]:
    """The run-scoped worker environment (the selfdev/m8 flow env, built in code)."""
    env = {k: v for k, v in base.items() if k != "AGENT_RUNTIME_GIT_TOKEN"}
    deploy = cfg.deploy
    env.update(
        {
            "AGENT_RUNTIME_API_URL": settings.api_url,
            "AGENT_RUNTIME_API_TOKEN": secrets.worker_api_token or secrets.api_token,
            "AGENT_RUNTIME_RUN_ID": str(run_id),
            "AGENT_RUNTIME_AGENT": "swarm",
            "AGENT_RUNTIME_REPO_URL": f"{settings.gitea_url}/{repo}.git",
            "AGENT_RUNTIME_BASE_BRANCH": "main",
            "AGENT_RUNTIME_SANDBOX_BACKEND": "k3s",
            "AGENT_RUNTIME_K8S_NAMESPACE": "agents",
            "AGENT_RUNTIME_LITELLM_BASE_URL": "http://litellm.platform.svc.cluster.local:4000/v1",
            "AGENT_RUNTIME_LITELLM_API_KEY": secrets.litellm_key,
            "AGENT_RUNTIME_MODEL": settings.model,
            "AGENT_RUNTIME_SESSION_TIMEOUT_SECONDS": str(settings.session_timeout_s),
            "AGENT_RUNTIME_MCP_TASK_SOURCE_URL": "http://task-reader.agents.svc.cluster.local:8000/mcp",
            "AGENT_RUNTIME_MCP_LLM_URL": "http://llm.agents.svc.cluster.local:8000/mcp",
            "AGENT_RUNTIME_MCP_OBSERVABILITY_URL": (
                "http://observability.agents.svc.cluster.local:8000/mcp"
            ),
            "AGENT_RUNTIME_TASK_REF": task_ref,
            "AGENT_RUNTIME_TASK_SOURCE_URL": "http://localhost:30085/mcp",
            "AGENT_RUNTIME_TASK_SOURCE_TOKEN": secrets.task_source_token,
            "AGENT_RUNTIME_GATES_ENABLED": "1",
            "AGENT_RUNTIME_CI_TIMEOUT_S": "1500",
            "AGENT_RUNTIME_HITL_POLL_S": "15",
            "AGENT_RUNTIME_REVIEWER_BASE_URL": settings.host_litellm_url,
            "AGENT_RUNTIME_PROMETHEUS_URL": "http://localhost:30090",
            "AGENT_RUNTIME_DEPLOY_ENABLED": "1" if deploy else "0",
            "AGENT_RUNTIME_VAULT_ROOT_TOKEN_PATH": str(
                settings.repo_root / "infra" / ".vault-root-token"
            ),
            "AGENT_RUNTIME_WORKSPACE_DIR": str(settings.workspace_dir / f"run-{run_id}"),
        }
    )
    if deploy:
        env["AGENT_RUNTIME_DEPLOY_APP"] = deploy.app
        env["AGENT_RUNTIME_DEPLOY_NAMESPACE"] = deploy.namespace
    return env


async def run_loop(
    tick: Callable[[], Awaitable[list[str]]],
    *,
    interval_s: float,
    stop: asyncio.Event,
    log: Callable[[str], None],
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Tick until ``stop``; a failing tick is logged, never fatal."""

    async def _default_sleep(seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            return

    pause = sleep or _default_sleep
    while not stop.is_set():
        try:
            for line in await tick():
                log(line)
        except Exception as exc:  # one bad tick (Gitea/API down) must not stop the platform
            log(f"tick failed: {type(exc).__name__}: {exc}")
        if stop.is_set():
            break
        await pause(interval_s)


class StandPorts:
    """``dispatcher.Ports`` over the live stand: Gitea, agent_api, LiteLLM, processes."""

    def __init__(
        self,
        settings: DaemonSettings,
        secrets: Secrets,
        api: httpx.AsyncClient,
        gitea: httpx.AsyncClient,
        llm: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.secrets = secrets
        self.api = api
        self.gitea = gitea
        self.llm = llm
        self._worker_seq: dict[int, int] = {}

    async def list_repos(self) -> list[str]:
        repos: list[str] = []
        page = 1
        while True:
            response = await self.gitea.get(
                f"/api/v1/orgs/{self.settings.org}/repos", params={"limit": 50, "page": page}
            )
            response.raise_for_status()
            batch = response.json()
            repos += [str(r["full_name"]) for r in batch if not r.get("archived")]
            if len(batch) < 50:
                return repos
            page += 1

    async def read_config(self, repo: str) -> str | None:
        response = await self.gitea.get(
            f"/api/v1/repos/{repo}/raw/.platform/project.yaml", params={"ref": "main"}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    async def list_tasks(self, repo: str) -> list[Task]:
        response = await self.gitea.get(
            f"/api/v1/repos/{repo}/issues",
            params={"state": "open", "type": "issues", "labels": INTAKE_LABEL, "limit": 50},
        )
        response.raise_for_status()
        return [
            Task(repo, int(i["number"]), str(i["title"]), str(i.get("body") or ""), True)
            for i in response.json()
        ]

    async def comment(self, repo: str, number: int, text: str) -> None:
        response = await self.gitea.post(
            f"/api/v1/repos/{repo}/issues/{number}/comments", json={"body": text}
        )
        response.raise_for_status()

    async def plan(self, run_id: int, task: Task, cfg: ProjectConfig) -> int:
        plans = await plan_task(
            self.llm,
            base_url=self.settings.host_litellm_url,
            api_key=self.secrets.litellm_key,
            model=self.settings.planner_model,
            title=task.title,
            body=task.body,
            allowed=cfg.paths,
        )
        ids = await post_plan(self.api, run_id=run_id, plans=plans, done_cmd=cfg.done_when)
        return len(ids)

    def spawn(self, run_id: int, task_ref: str, cfg: ProjectConfig, workers: int) -> list[Any]:
        repo = task_ref.rsplit("#", 1)[0]
        base = build_worker_env(
            self.settings,
            self.secrets,
            run_id=run_id,
            repo=repo,
            cfg=cfg,
            base=dict(os.environ),
            task_ref=task_ref,
        )
        root = Path(base["AGENT_RUNTIME_WORKSPACE_DIR"])
        procs: list[Any] = []
        for _ in range(workers):
            index = self._worker_seq.get(run_id, 0) + 1
            self._worker_seq[run_id] = index
            (root / f"w{index}").mkdir(parents=True, exist_ok=True)
            with (root / f"w{index}.log").open("a", encoding="utf-8") as log:
                procs.append(
                    subprocess.Popen(  # noqa: S603 - fixed argv, env from build_worker_env
                        [sys.executable, "-m", "agent_runtime.worker"],
                        env=worker_env(base, root=root, index=index),
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                )
        return procs

    async def judge(self, run_id: int, repo: str, cfg: ProjectConfig) -> JudgeVerdict:
        return await judge_run(
            self.api,
            run_id=run_id,
            project=repo,
            check_main=_check_main_factory(
                f"{self.settings.gitea_url}/{repo}.git",
                "main",
                cfg.done_when,
                self.secrets.gitea_token or None,
                workdir=self.settings.workspace_dir / "judge",
            ),
        )

    async def api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await self.api.get(path, params=params)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    async def api_post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        response = await self.api.post(path, json=json)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body


def stop_workers(
    procs: list[Any],
    grace_s: float,
    *,
    now: Callable[[], float] = time.monotonic,
    pause: Callable[[float], None] = time.sleep,
) -> None:
    """SIGTERM every live process, wait up to ``grace_s`` for them, then SIGKILL (M12)."""
    live = [p for p in procs if p.poll() is None]
    for proc in live:
        proc.terminate()
    deadline = now() + grace_s
    while any(p.poll() is None for p in live) and now() < deadline:
        pause(0.5)
    for proc in live:
        if proc.poll() is None:
            proc.kill()


def _kubectl(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed kubectl argv
        argv, input=stdin, capture_output=True, text=True, check=False, timeout=60
    )


def identity_tokens(fields: dict[str, str]) -> tuple[str, str, str]:
    """(daemon, worker, poller) service tokens from ``secret/platform/api`` (M12).

    A stand bootstrapped before M12 has only the shared ``token``: every identity
    falls back to it until ``bootstrap.sh platform`` writes the per-identity ones.
    """
    shared = fields.get("token", "")
    return (
        fields.get("daemon") or shared,
        fields.get("worker") or shared,
        fields.get("poller") or shared,
    )


def load_secrets(settings: DaemonSettings) -> Secrets:
    root = (settings.repo_root / "infra" / ".vault-root-token").read_text(encoding="utf-8").strip()

    def read(path: str, name: str = "token") -> str:
        return read_gitea_token(_kubectl, root_token=root, path=path, field=name)

    def optional(path: str, name: str) -> str:
        try:
            return read(path, name)
        except VaultBrokerError:
            return ""

    api_fields = {
        name: optional(SERVICE_TOKEN_PATH, name) for name in ("daemon", "worker", "poller")
    }
    api_fields["token"] = read(SERVICE_TOKEN_PATH)
    daemon, worker, poller = identity_tokens(api_fields)
    return Secrets(
        litellm_key=read(LITELLM_MASTER_PATH, "master-key"),
        gitea_token=read(AGENT_TOKEN_PATH),
        api_token=daemon,
        worker_api_token=worker,
        poller_api_token=poller,
        task_source_token=read(TASK_SOURCE_TOKEN_PATH),
    )


def poller_env(settings: DaemonSettings, secrets: Secrets, base: dict[str, str]) -> dict[str, str]:
    """The automation poller's environment: its own service identity (M12)."""
    env = dict(base)
    env.update(
        {
            "AGENT_RUNTIME_API_URL": settings.api_url,
            "AGENT_RUNTIME_API_TOKEN": secrets.poller_api_token or secrets.api_token,
            "AUTOMATIONS_GITEA_URL": settings.gitea_url,
            "AUTOMATIONS_GITEA_TOKEN": secrets.gitea_token,
            "AUTOMATIONS_WORKDIR": str(settings.workspace_dir / "automations"),
        }
    )
    return env


def _start_poller(settings: DaemonSettings, secrets: Secrets) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "agent_runtime.automations", "poll", "--interval", "30"],
        env=poller_env(settings, secrets, dict(os.environ)),
    )


async def _main() -> int:
    settings = DaemonSettings()
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    secrets = load_secrets(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    def log(line: str) -> None:
        print(f"daemon: {line}", flush=True)

    poller = _start_poller(settings, secrets)
    log(f"automation poller pid={poller.pid}")
    async with (
        httpx.AsyncClient(
            base_url=settings.api_url,
            headers={"Authorization": f"Bearer {secrets.api_token}"},
            timeout=60.0,
        ) as api,
        httpx.AsyncClient(
            base_url=settings.gitea_url,
            headers={"Authorization": f"token {secrets.gitea_token}"},
            timeout=60.0,
        ) as gitea,
        httpx.AsyncClient(timeout=300.0) as llm,
    ):
        dispatcher = Dispatcher(
            StandPorts(settings, secrets, api, gitea, llm),
            max_active_runs=settings.max_active_runs,
            sandbox_cap=settings.sandbox_cap,
        )

        async def tick() -> list[str]:
            nonlocal poller
            if poller.poll() is not None:
                poller = _start_poller(settings, secrets)
                return [f"automation poller restarted (pid={poller.pid})", *await dispatcher.tick()]
            return await dispatcher.tick()

        log(f"dispatcher up: org={settings.org} api={settings.api_url}")
        await run_loop(tick, interval_s=settings.interval_s, stop=stop, log=log)
    # M12: workers get SIGTERM, release the vertex in flight and tear their
    # sandbox down; the next daemon re-spawns them for the requeued vertices.
    procs = [poller, *(p for ps in dispatcher.procs.values() for p in ps)]
    await asyncio.to_thread(stop_workers, procs, settings.shutdown_grace_s)
    log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
