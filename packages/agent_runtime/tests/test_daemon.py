"""Platform daemon (M11): worker env assembly, secret hygiene, resilient loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_runtime.daemon import DaemonSettings, Secrets, build_worker_env, run_loop
from agent_runtime.project_config import parse_project_config

CFG = 'intake: true\npaths: ["src/**"]\ndone_when: "pytest"\n'
DEPLOY_CFG = CFG + "deploy: {app: isv-product, namespace: prod}\n"
SECRETS = Secrets(litellm_key="mk-secret-value")


def _env(cfg_text: str, **base: str) -> dict[str, str]:
    settings = DaemonSettings(repo_root=Path("/repo"), workspace_dir=Path("/repo/ws"))
    return build_worker_env(
        settings,
        SECRETS,
        run_id=5,
        repo="swarm-lab/demo",
        cfg=parse_project_config(cfg_text),
        base=base,
    )


def test_worker_env_core() -> None:
    env = _env(CFG, PATH="/usr/bin")
    assert env["PATH"] == "/usr/bin"
    assert env["AGENT_RUNTIME_RUN_ID"] == "5"
    assert env["AGENT_RUNTIME_REPO_URL"] == "http://localhost:30080/swarm-lab/demo.git"
    assert env["AGENT_RUNTIME_SANDBOX_BACKEND"] == "k3s"
    assert env["AGENT_RUNTIME_GATES_ENABLED"] == "1"
    assert env["AGENT_RUNTIME_LITELLM_API_KEY"] == "mk-secret-value"
    assert env["AGENT_RUNTIME_API_URL"] == "http://localhost:30400"
    # the pinned worktree lacks the gitignored token file: point at the repo root
    assert env["AGENT_RUNTIME_VAULT_ROOT_TOKEN_PATH"] == "/repo/infra/.vault-root-token"
    assert env["AGENT_RUNTIME_DEPLOY_ENABLED"] == "0"


def test_worker_env_has_no_git_token() -> None:
    env = _env(CFG, AGENT_RUNTIME_GIT_TOKEN="leaked-admin-token")
    assert "AGENT_RUNTIME_GIT_TOKEN" not in env  # the worker resolves swarm-agent from OpenBao


def test_worker_env_task_ref_is_per_run() -> None:
    settings = DaemonSettings(repo_root=Path("/repo"), workspace_dir=Path("/repo/ws"))
    env = build_worker_env(
        settings,
        SECRETS,
        run_id=5,
        repo="swarm-lab/demo",
        cfg=parse_project_config(CFG),
        base={},
        task_ref="swarm-lab/demo#7",
    )
    assert env["AGENT_RUNTIME_TASK_REF"] == "swarm-lab/demo#7"


def test_worker_env_deploy_from_config() -> None:
    env = _env(DEPLOY_CFG)
    assert env["AGENT_RUNTIME_DEPLOY_ENABLED"] == "1"
    assert env["AGENT_RUNTIME_DEPLOY_APP"] == "isv-product"
    assert env["AGENT_RUNTIME_DEPLOY_NAMESPACE"] == "prod"


def test_secrets_not_in_repr() -> None:
    assert "mk-secret-value" not in repr(SECRETS)
    assert "mk-secret-value" not in str(SECRETS)


async def test_daemon_loop_survives_tick_error() -> None:
    calls: list[int] = []
    logged: list[str] = []
    stop = asyncio.Event()

    async def tick() -> list[str]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("gitea down")
        if len(calls) == 3:
            stop.set()
        return ["ok"]

    async def no_sleep(_: float) -> None:
        return None

    await run_loop(tick, interval_s=30, stop=stop, log=logged.append, sleep=no_sleep)
    assert len(calls) == 3
    assert any("gitea down" in line for line in logged)
    assert logged.count("ok") == 2


def test_worker_env_carries_the_service_token() -> None:
    settings = DaemonSettings(repo_root=Path("/repo"), workspace_dir=Path("/repo/ws"))
    env = build_worker_env(
        settings,
        Secrets(litellm_key="mk", api_token="svc-token"),
        run_id=1,
        repo="swarm-lab/demo",
        cfg=parse_project_config(CFG),
        base={},
    )
    assert env["AGENT_RUNTIME_API_TOKEN"] == "svc-token"
    assert "svc-token" not in repr(Secrets(api_token="svc-token"))


def test_worker_env_carries_the_task_source_token() -> None:
    settings = DaemonSettings(repo_root=Path("/repo"), workspace_dir=Path("/repo/ws"))
    env = build_worker_env(
        settings,
        Secrets(litellm_key="mk", task_source_token="ts-token"),
        run_id=1,
        repo="swarm-lab/demo",
        cfg=parse_project_config(CFG),
        base={},
    )
    assert env["AGENT_RUNTIME_TASK_SOURCE_TOKEN"] == "ts-token"
    assert "ts-token" not in repr(Secrets(task_source_token="ts-token"))


def test_workers_get_the_worker_identity_token() -> None:
    settings = DaemonSettings(repo_root=Path("/repo"), workspace_dir=Path("/repo/ws"))
    env = build_worker_env(
        settings,
        Secrets(litellm_key="mk", api_token="daemon-tok", worker_api_token="worker-tok"),
        run_id=1,
        repo="swarm-lab/demo",
        cfg=parse_project_config(CFG),
        base={},
    )
    assert env["AGENT_RUNTIME_API_TOKEN"] == "worker-tok"


def test_identity_tokens_fall_back_to_the_shared_token() -> None:
    from agent_runtime.daemon import identity_tokens

    shared = {"token": "t0"}
    assert identity_tokens(shared) == ("t0", "t0", "t0")
    split = {"token": "t0", "daemon": "d", "worker": "w", "poller": "p"}
    assert identity_tokens(split) == ("d", "w", "p")


def test_poller_env_carries_the_poller_token() -> None:
    from agent_runtime.daemon import poller_env

    settings = DaemonSettings(repo_root=Path("/repo"), workspace_dir=Path("/repo/ws"))
    env = poller_env(settings, Secrets(api_token="d", poller_api_token="p", gitea_token="g"), {})
    assert env["AGENT_RUNTIME_API_TOKEN"] == "p"
    assert env["AUTOMATIONS_GITEA_TOKEN"] == "g"
