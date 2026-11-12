"""Worker integration tests for the k3s sandbox backend (M4b)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from agent_runtime.executor import Executor
from agent_runtime.kube_sandbox import KubeSandboxExecutorFactory
from agent_runtime.opencode_executor import OpencodeExecutor
from agent_runtime.settings import WorkerSettings
from agent_runtime.worker import (
    build_executor,
    prepare_settings,
    resolve_gitea_token,
    startup_executor,
)

Run = Any  # kubectl runner callable shape (argv, stdin) -> CompletedProcess


def _settings(tmp_path: Path, **overrides: Any) -> WorkerSettings:
    base: dict[str, Any] = {
        "api_url": "http://127.0.0.1:8471",
        "run_id": 30,
        "repo_url": "http://127.0.0.1:30080/swarm-lab/target-scratch",
        "litellm_base_url": "http://litellm.platform.svc.cluster.local:4000",
        "model": "litellm/qwen3",
        "litellm_api_key": "sk-master",
        "workspace_dir": tmp_path / "ws",
    }
    base.update(overrides)
    return WorkerSettings(**base)


def test_build_executor_docker_returns_opencode_executor(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **dict(__import__("os").environ),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@l",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@l",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True, env=env)
    (tmp_path / "ws" / "run-30").mkdir(parents=True)
    settings = _settings(tmp_path)
    executor = build_executor(settings, repo_dir=repo, gitea_token="")
    assert isinstance(executor, OpencodeExecutor)
    assert isinstance(executor, Executor)


def test_resolve_gitea_token_prefers_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_broker(*a: Any, **kw: Any) -> str:
        pytest.fail("broker must not run when git_token is set")
        return ""

    monkeypatch.setattr("agent_runtime.worker.read_gitea_token", must_not_broker)
    settings = _settings(tmp_path, sandbox_backend="k3s", git_token="explicit-token")
    assert resolve_gitea_token(settings) == "explicit-token"


def test_resolve_gitea_token_brokers_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_broker(kube_runner: Any, *, root_token: str, namespace: str, pod_label: str) -> str:
        captured.update(root_token=root_token, namespace=namespace, pod_label=pod_label)
        return "gitea-brokered-TOKEN"

    monkeypatch.setattr("agent_runtime.worker.read_gitea_token", fake_broker)
    token_file = tmp_path / "root-token"
    token_file.write_text("vault-root-SECRET\n", encoding="utf-8")
    settings = _settings(
        tmp_path,
        sandbox_backend="k3s",
        git_token="",
        vault_root_token_path=token_file,
        openbao_namespace="platform-x",
        openbao_pod_label="app.kubernetes.io/name=openbao-x",
    )
    assert resolve_gitea_token(settings) == "gitea-brokered-TOKEN"
    assert captured["root_token"] == "vault-root-SECRET"
    assert captured["namespace"] == "platform-x", "openbao_namespace must reach the broker"
    assert captured["pod_label"] == "app.kubernetes.io/name=openbao-x"


def test_prepare_settings_carries_brokered_token_into_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: the Vault-only path cloned with the brokered token, but the
    # loop's fetch/apply/push read settings.git_token (empty) -> "could not
    # read Username" on a host without a git credential helper.
    monkeypatch.setattr(
        "agent_runtime.worker.read_gitea_token",
        lambda *a, **k: "gitea-brokered-TOKEN",
    )
    settings = _settings(
        tmp_path, sandbox_backend="k3s", git_token="", vault_root_token="inline-root"
    )
    prepared = prepare_settings(settings)
    assert prepared.git_token == "gitea-brokered-TOKEN"
    assert settings.git_token == "", "the input settings must not be mutated"


def test_prepare_settings_docker_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_broker(*a: Any, **k: Any) -> str:
        pytest.fail("the docker backend must not broker a token")

    monkeypatch.setattr("agent_runtime.worker.read_gitea_token", no_broker)
    settings = _settings(tmp_path, sandbox_backend="docker", git_token="")
    assert prepare_settings(settings).git_token == ""


def test_resolve_gitea_token_accepts_inline_root_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_broker(kube_runner: Any, *, root_token: str, namespace: str, pod_label: str) -> str:
        captured["root_token"] = root_token
        return "brokered"

    monkeypatch.setattr("agent_runtime.worker.read_gitea_token", fake_broker)
    settings = _settings(
        tmp_path, sandbox_backend="k3s", git_token="", vault_root_token="inline-root"
    )
    assert resolve_gitea_token(settings) == "brokered"
    assert captured["root_token"] == "inline-root"


def test_resolve_gitea_token_requires_a_root_source(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        sandbox_backend="k3s",
        git_token="",
        vault_root_token="",
        vault_root_token_path=tmp_path / "missing",
    )
    with pytest.raises(ValueError, match="vault"):
        resolve_gitea_token(settings)


def test_build_executor_k3s_requires_resolved_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path, sandbox_backend="k3s")
    with pytest.raises(TypeError):
        build_executor(settings, repo_dir=tmp_path / "repo")  # type: ignore[call-arg]


def test_build_executor_k3s_returns_factory(tmp_path: Path) -> None:
    settings = _settings(tmp_path, sandbox_backend="k3s")
    executor = build_executor(settings, repo_dir=tmp_path / "repo", gitea_token="t")
    assert isinstance(executor, KubeSandboxExecutorFactory)


def test_startup_reconciles_orphan_pods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker startup (k3s) sweeps orphaned sandbox objects BEFORE the first claim."""
    calls: list[str] = []

    def fake_reconcile(*a: Any, **kw: Any) -> int:
        calls.append(f"reconcile:{kw.get('max_age_seconds')}")
        return 0

    monkeypatch.setattr("agent_runtime.worker.reconcile_orphans", fake_reconcile)
    settings = _settings(tmp_path, sandbox_backend="k3s")
    # The reconcile ride-along: resolving the token is part of startup.
    monkeypatch.setattr(
        "agent_runtime.worker.resolve_gitea_token",
        lambda *a, **kw: "tok",
    )
    executor = startup_executor(settings)
    assert isinstance(executor, KubeSandboxExecutorFactory)
    assert calls == ["reconcile:600.0"], "2 x lease_ttl_seconds sweep window"


async def test_built_k3s_executor_starts_sandbox_per_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end wiring: factory.start spawns a sandbox and attaches the adapter."""
    import agent_runtime.kube_sandbox as ks
    import httpx
    from agent_runtime.executor import ExecutionRequest

    captured: dict[str, Any] = {}

    class SpyExecutor(OpencodeExecutor):
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(ks, "OpencodeExecutor", SpyExecutor)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/session"):
            return httpx.Response(200, json={"data": {"id": "ses_t"}})
        if request.url.path == "/api/event":
            body = (
                'data: {"id":"e1","type":"session.execution.succeeded",'
                '"data":{"sessionID":"ses_t"}}\n\n'
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        if "diff" in request.url.path:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={})

    def fake_kube(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        hay = " ".join(argv) + " " + (stdin or "")
        if "get pod" in hay:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    '{"status":{"phase":"Running","conditions":[{"type":"Ready","status":"True"}]}}'
                ),
                stderr="",
            )
        if "logs" in hay:
            return subprocess.CompletedProcess(
                argv, 0, stdout="server password pod-pass-123\n", stderr=""
            )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    class TransportedFactory(KubeSandboxExecutorFactory):
        def __init__(self, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(**kwargs)

    monkeypatch.setattr("agent_runtime.worker.KubeSandboxExecutorFactory", TransportedFactory)

    settings = _settings(
        tmp_path,
        sandbox_backend="k3s",
        git_token="explicit-token",
    )
    executor = build_executor(
        settings, repo_dir=tmp_path / "repo", gitea_token="explicit-token", kube_runner=fake_kube
    )
    request = ExecutionRequest(
        run_id=30,
        vertex_id=29,
        name="v",
        prompt="p",
        paths_allow=[],
        done_when='{"checks":[]}',
        repo_url=settings.repo_url,
        work_branch="agent/vertex-29",
    )
    session = await executor.start(request)
    assert isinstance(session, ks.SandboxSession)
    # The real assertion: the adapter was built from the sandbox handle.
    assert captured["base_url"].startswith("http://127.0.0.1:301")
    assert captured["auth"] == ("opencode", "pod-pass-123")
    await session.stop()
