"""Worker-side wiring of the deployer (M6): after the gates merge, roll out main.

The production runner clones the merged ``main`` of the task's repo, builds and
deploys it as a canary (:mod:`agent_runtime.deployer`) with the pre-rollout
baseline from Prometheus and HITL #3 through agent_api.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import subprocess
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agent_runtime.deployer import DeployVerdict, KubectlPort, deploy
from agent_runtime.gitops import _git_env
from agent_runtime.hitl_client import HITLClient
from agent_runtime.prom import Slo, service_slo
from agent_runtime.settings import WorkerSettings

DeployRunner = Callable[[list[str]], Awaitable[DeployVerdict]]


def deploy_paths_changed(changed: list[str]) -> bool:
    """A change under ``deploy/`` needs HITL #3 (agents cannot make one today)."""
    return any(p == "deploy" or p.startswith("deploy/") for p in changed)


def _clone_main(repo_url: str, branch: str, dest: Path, token: str | None) -> str:
    env = _git_env(token)
    subprocess.run(  # noqa: S603, S607
        ["git", "clone", "-q", "--depth", "1", "--branch", branch, repo_url, str(dest)],
        env=env,
        check=True,
        capture_output=True,
    )
    head = subprocess.run(  # noqa: S603, S607
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return head.stdout.strip()


# Workers of one daemon share the container's /tmp: one canary per app at a time.
DEPLOY_LOCK_DIR = Path(tempfile.gettempdir()) / "agent-runtime-deploy-locks"


@contextlib.asynccontextmanager
async def deploy_lock(app: str, lock_dir: Path = DEPLOY_LOCK_DIR) -> AsyncIterator[None]:
    """Serialize rollouts of ``app`` across worker processes (M11, found live: two
    concurrent canaries of one app — one verdict waited for a hash the other
    rollout had replaced)."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = (lock_dir / f"{app}.lock").open("w")
    try:
        await asyncio.to_thread(fcntl.flock, handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


async def already_live(kube: Any, sha: str) -> bool:
    """The rollout already runs this commit and is Healthy (a sibling vertex of
    the same run rolled the same ``main`` out while we waited for the lock)."""
    rollout = await kube.rollout()
    containers = rollout.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    image = str(containers[0].get("image", "")) if containers else ""
    phase = rollout.get("status", {}).get("phase")
    return image.endswith(f":{sha[:7]}") and phase == "Healthy"


def default_deploy_runner(settings: WorkerSettings, token: str | None) -> DeployRunner:
    async def run(changed: list[str]) -> DeployVerdict:
        async with deploy_lock(settings.deploy_app):
            return await _run_locked(changed)

    async def _run_locked(changed: list[str]) -> DeployVerdict:
        # Clone INSIDE the lock: the newest main, after any sibling's rollout.
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            sha = await asyncio.to_thread(
                _clone_main, settings.repo_url, settings.base_branch, repo_dir, token
            )
            kube = KubectlPort(namespace=settings.deploy_namespace)
            async with httpx.AsyncClient(timeout=30.0) as http:

                async def slo() -> Slo:
                    return await service_slo(
                        http,
                        settings.prometheus_url,
                        namespace=settings.deploy_namespace,
                        service=settings.deploy_app,
                    )

                if not deploy_paths_changed(changed) and await already_live(kube, sha):
                    now = datetime.now(UTC).isoformat(timespec="seconds")
                    image = f"{settings.deploy_app}:{sha[:7]}"
                    return DeployVerdict(
                        status="succeeded",
                        image=image,
                        previous_image=image,
                        revision=f"{sha[:7]} already live (rolled out by a sibling vertex)",
                        baseline=await slo(),
                        started=now,
                        finished=now,
                    )
                return await deploy(
                    sha,
                    repo_dir,
                    kube=kube,
                    slo=slo,
                    hitl=HITLClient(http, settings.api_url),
                    deploy_paths_changed=deploy_paths_changed(changed),
                    timeout_s=settings.deploy_timeout_s,
                )

    return run
