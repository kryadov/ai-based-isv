"""Worker-side wiring of the deployer (M6): after the gates merge, roll out main.

The production runner clones the merged ``main`` of the task's repo, builds and
deploys it as a canary (:mod:`agent_runtime.deployer`) with the pre-rollout
baseline from Prometheus and HITL #3 through agent_api.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

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


def default_deploy_runner(settings: WorkerSettings, token: str | None) -> DeployRunner:
    async def run(changed: list[str]) -> DeployVerdict:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            sha = await asyncio.to_thread(
                _clone_main, settings.repo_url, settings.base_branch, repo_dir, token
            )
            async with httpx.AsyncClient(timeout=30.0) as http:

                async def slo() -> Slo:
                    return await service_slo(
                        http,
                        settings.prometheus_url,
                        namespace=settings.deploy_namespace,
                        service=settings.deploy_app,
                    )

                return await deploy(
                    sha,
                    repo_dir,
                    kube=KubectlPort(namespace=settings.deploy_namespace),
                    slo=slo,
                    hitl=HITLClient(http, settings.api_url),
                    deploy_paths_changed=deploy_paths_changed(changed),
                    timeout_s=settings.deploy_timeout_s,
                )

    return run
