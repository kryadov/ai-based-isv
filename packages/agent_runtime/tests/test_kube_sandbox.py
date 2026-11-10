"""Tests for the k3s sandbox lifecycle over the kubectl runner seam."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_runtime.executor import ExecutionStatus
from agent_runtime.kube_sandbox import (
    KubeSandbox,
    KubeSandboxError,
    in_cluster_clone_url,
    pod_manifest,
    reconcile_orphans,
    secret_manifest,
    service_manifest,
)
from agent_runtime.opencode_executor import OpencodeExecutor

Run = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


class FakeKube:
    """Scripted kubectl runner: records (argv, stdin), returns canned results.

    Script entries are consumed FIFO: each call takes the FIRST unconsumed
    entry whose pattern matches argv+stdin; without a match the call succeeds
    (rc=0, empty output). Patterns may match manifest kinds because apply
    calls carry the kind in stdin, not in argv.
    """

    def __init__(self, script: list[tuple[str, subprocess.CompletedProcess[str]]] | None = None):
        self.calls: list[tuple[list[str], str | None]] = []
        self.script = list(script or [])

    def __call__(self, argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, stdin))
        hay = " ".join(argv) + " " + (stdin or "")
        for i, (pattern, result) in enumerate(self.script):
            if all(part in hay for part in pattern.split()):
                self.script.pop(i)
                return result
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    def calls_with(self, *parts: str) -> list[tuple[list[str], str | None]]:
        return [
            c for c in self.calls if all(p in (" ".join(c[0]) + " " + (c[1] or "")) for p in parts)
        ]

    def first_stdin(self, *parts: str) -> dict[str, Any]:
        call = self.calls_with(*parts)[0]
        assert call[1] is not None
        data: dict[str, Any] = json.loads(call[1])
        return data


def _sandbox_args() -> dict[str, Any]:
    return {
        "gitea_token": "gitea-admin-TOKEN-SECRET",
        "repo_url": "http://127.0.0.1:30080/swarm-lab/target-scratch",
        "base_branch": "main",
        "run_id": 30,
        "vertex_id": 29,
        "litellm_base_url": "http://litellm.platform.svc.cluster.local:4000",
        "litellm_api_key": "sk-master-KEY-SECRET",
        "model": "litellm/qwen3",
    }


def _ready_pod_json() -> str:
    return json.dumps(
        {
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True"}],
            }
        }
    )


def _pod_list_json(entries: list[dict[str, Any]]) -> str:
    return json.dumps({"items": entries})


def _pod_entry(name: str, created: str) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": {"agent-runtime.io/managed": "yes"},
            "creationTimestamp": created,
        }
    }


def test_in_cluster_clone_url_rewrites_authority_keeps_path() -> None:
    url = in_cluster_clone_url("http://127.0.0.1:30080/swarm-lab/target-scratch")
    assert url == "http://gitea-http.platform.svc.cluster.local:3000/swarm-lab/target-scratch"


def test_pod_manifest_shape() -> None:
    from agent_runtime.kube_sandbox import SANDBOX_IMAGE

    assert SANDBOX_IMAGE.startswith("opencode-git:"), (
        "the sandbox must run the git-enabled derived image: the official image "
        "ships no git and the harness diff endpoint depends on it"
    )
    pod = pod_manifest(
        image="opencode-git:2.2.0",
        repo_url="http://127.0.0.1:30080/swarm-lab/target-scratch",
        base_branch="main",
        run_id=30,
        vertex_id=29,
        secret_name="agent-30-29",
        config_jsonc='{"model": "litellm/qwen3"}\n',
    )
    assert pod["metadata"]["name"] == "agent-30-29"
    assert pod["metadata"]["labels"]["agent-runtime.io/run-id"] == "30"
    assert pod["metadata"]["labels"]["agent-runtime.io/vertex-id"] == "29"
    init = pod["spec"]["initContainers"][0]
    assert init["image"].startswith("alpine/git")
    clone_script = " ".join(init["command"])
    assert "gitea-http.platform.svc.cluster.local:3000" in clone_script
    assert "run-30/vertex-29" in clone_script
    env_names = [e["name"] for e in init["env"]]
    assert "GITEA_TOKEN" in env_names
    token_env = next(e for e in init["env"] if e["name"] == "GITEA_TOKEN")
    assert token_env["valueFrom"]["secretKeyRef"]["name"] == "agent-30-29"
    main = pod["spec"]["containers"][0]
    main_json = json.dumps(main)
    assert "gitea-admin-TOKEN-SECRET" not in main_json
    assert main["workingDir"] == "/workspace/run-30/vertex-29"
    env_main = {e["name"]: e for e in main["env"]}
    assert "OPENCODE_DB" not in env_main, (
        "state must stay in the container: OPENCODE_DB on the PVC breaks snapshots"
    )
    assert env_main["LITELLM_API_KEY"]["valueFrom"]["secretKeyRef"]["key"] == "litellm-api-key"
    main_mounts = main["volumeMounts"]
    assert {"name": "workspace", "mountPath": "/workspace"} in main_mounts, (
        "the serve container must see the cloned repo on the PVC"
    )
    volume_names = [v["name"] for v in pod["spec"]["volumes"]]
    assert set(volume_names) == {"workspace", "config"}


def test_secret_and_service_manifests() -> None:
    secret = secret_manifest(
        name="agent-30-29",
        run_id=30,
        vertex_id=29,
        gitea_token="gitea-admin-TOKEN-SECRET",
        litellm_api_key="sk-master-KEY-SECRET",
        config_jsonc='{"model": "litellm/qwen3"}\n',
    )
    assert secret["stringData"]["gitea-token"] == "gitea-admin-TOKEN-SECRET"
    assert secret["stringData"]["litellm-api-key"] == "sk-master-KEY-SECRET"
    assert '"model": "litellm/qwen3"' in secret["stringData"]["opencode.jsonc"]
    svc = service_manifest(name="agent-30-29", run_id=30, vertex_id=29, node_port=30102)
    assert svc["spec"]["type"] == "NodePort"
    assert svc["spec"]["ports"][0]["nodePort"] == 30102
    assert svc["spec"]["ports"][0]["port"] == 4096


def test_start_happy_path_returns_handle() -> None:
    kube = FakeKube(
        [
            (
                "get svc",
                subprocess.CompletedProcess([], 0, stdout=json.dumps({"items": []}), stderr=""),
            ),
            (
                "get pod",
                subprocess.CompletedProcess([], 0, stdout=_ready_pod_json(), stderr=""),
            ),
            (
                "kubectl -n agents logs",
                subprocess.CompletedProcess(
                    [], 0, stdout="server password pod-secret-token\n", stderr=""
                ),
            ),
        ]
    )
    sandbox = KubeSandbox(kube_runner=kube, **_sandbox_args())
    handle = sandbox.start(timeout_seconds=5.0)
    assert handle.base_url == "http://127.0.0.1:30100"
    assert handle.password == "pod-secret-token"
    assert handle.pod_name == "agent-30-29"
    assert handle.workspace_pod_path == "/workspace/run-30/vertex-29"
    secret = kube.first_stdin("apply", "Secret")
    assert secret["stringData"]["gitea-token"] == "gitea-admin-TOKEN-SECRET"
    applied_pod = kube.first_stdin("apply", "Pod")
    assert applied_pod["spec"]["containers"][0]["image"].startswith("opencode-git:"), (
        "the spawned pod must use the git-enabled derived image"
    )
    assert kube.calls_with("delete") == [], "happy path must not tear anything down"


def test_nodeport_allocation_skips_used() -> None:
    used = json.dumps(
        {
            "items": [
                {
                    "spec": {
                        "ports": [
                            {"nodePort": 30100},
                            {"nodePort": 30101},
                        ]
                    }
                }
            ]
        }
    )
    kube = FakeKube(
        [
            ("get svc", subprocess.CompletedProcess([], 0, stdout=used, stderr="")),
            (
                "get pod",
                subprocess.CompletedProcess([], 0, stdout=_ready_pod_json(), stderr=""),
            ),
            (
                "kubectl -n agents logs",
                subprocess.CompletedProcess([], 0, stdout="server password tok\n", stderr=""),
            ),
        ]
    )
    KubeSandbox(kube_runner=kube, **_sandbox_args()).start(timeout_seconds=5.0)
    svc = kube.first_stdin("apply", "Service")
    assert svc["spec"]["ports"][0]["nodePort"] == 30102


def test_nodeport_conflict_retries_next() -> None:
    conflict = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="error: provided port is already allocated"
    )
    kube = FakeKube(
        [
            (
                "get svc",
                subprocess.CompletedProcess([], 0, stdout=json.dumps({"items": []}), stderr=""),
            ),
            ("apply Service", conflict),
            (
                "get pod",
                subprocess.CompletedProcess([], 0, stdout=_ready_pod_json(), stderr=""),
            ),
            (
                "kubectl -n agents logs",
                subprocess.CompletedProcess([], 0, stdout="server password tok\n", stderr=""),
            ),
        ]
    )
    KubeSandbox(kube_runner=kube, **_sandbox_args()).start(timeout_seconds=5.0)
    svc_calls = kube.calls_with("apply", "Service")
    assert len(svc_calls) == 2, "retry expected after a NodePort conflict"
    second = json.loads(svc_calls[1][1] or "{}")
    assert second["spec"]["ports"][0]["nodePort"] == 30101


def test_stop_deletes_service_pod_secret_idempotent() -> None:
    kube = FakeKube(
        [
            (
                "get pod",
                subprocess.CompletedProcess([], 0, stdout=_ready_pod_json(), stderr=""),
            ),
            (
                "kubectl -n agents logs",
                subprocess.CompletedProcess([], 0, stdout="server password tok\n", stderr=""),
            ),
        ]
    )
    sandbox = KubeSandbox(kube_runner=kube, **_sandbox_args())
    sandbox.start(timeout_seconds=5.0)
    sandbox.stop()
    sandbox.stop()
    deletes = kube.calls_with("delete")
    assert len(deletes) == 6, "three deletes per stop, idempotent across two stops"
    targets = " ".join(" ".join(argv) for argv, _ in deletes)
    for name in ("service/agent-30-29", "pod/agent-30-29", "secret/agent-30-29"):
        assert name in targets


def test_start_failure_cleans_up_secret_and_pod() -> None:
    failed_pod = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"status": {"phase": "Failed"}}), stderr=""
    )
    kube = FakeKube(
        [
            ("get pod", failed_pod),
        ]
    )
    sandbox = KubeSandbox(kube_runner=kube, **_sandbox_args())
    with pytest.raises(KubeSandboxError):
        sandbox.start(timeout_seconds=5.0)
    targets = " ".join(" ".join(argv) for argv, _ in kube.calls_with("delete"))
    assert "secret/agent-30-29" in targets
    assert "pod/agent-30-29" in targets
    assert "service/agent-30-29" in targets


def test_reconcile_orphans_deletes_old_labeled_pods() -> None:
    kube = FakeKube(
        [
            (
                "get pods",
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=_pod_list_json(
                        [
                            _pod_entry("agent-1-1", "2026-11-09T00:00:00Z"),
                            _pod_entry("agent-2-2", "2099-01-01T00:00:00Z"),
                        ]
                    ),
                    stderr="",
                ),
            ),
        ]
    )
    removed = reconcile_orphans(kube, namespace="agents", max_age_seconds=3600.0)
    assert removed == 1
    deleted = " ".join(" ".join(argv) for argv, _ in kube.calls_with("delete pod"))
    assert "agent-1-1" in deleted
    assert "agent-2-2" not in deleted


def test_runner_error_raises_kube_sandbox_error_without_token_echo() -> None:
    kube = FakeKube(
        [
            ("apply Secret", subprocess.CompletedProcess([], 1, stdout="", stderr="boom")),
        ]
    )
    sandbox = KubeSandbox(kube_runner=kube, **_sandbox_args())
    with pytest.raises(KubeSandboxError) as excinfo:
        sandbox.start(timeout_seconds=5.0)
    assert "gitea-admin-TOKEN-SECRET" not in str(excinfo.value)
    deletes = " ".join(" ".join(argv) for argv, _ in kube.calls_with("delete"))
    assert "secret/agent-30-29" in deletes


class _StubSession:
    """Minimal ExecutionSession double for wiring tests."""

    def __init__(self) -> None:
        self.stopped = False

    def events(self) -> AsyncIterator[Any]:
        async def _empty() -> AsyncIterator[Any]:
            return
            yield  # pragma: no cover  (unreachable generator formality)

        return _empty()

    async def wait_done(self) -> Any:
        return ExecutionStatus.DONE

    async def diff(self) -> str:
        return ""

    async def status(self) -> ExecutionStatus:
        return ExecutionStatus.DONE

    async def stop(self) -> None:
        self.stopped = True

    @property
    def result(self) -> Any:
        return None


def _opencode_mock(request: httpx.Request) -> httpx.Response:
    """Minimal opencode serve: session create, SSE terminal event, diff."""
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


def _factory_request() -> Any:
    from agent_runtime.executor import ExecutionRequest

    return ExecutionRequest(
        run_id=30,
        vertex_id=29,
        name="v",
        prompt="p",
        paths_allow=[],
        done_when='{"checks":[]}',
        repo_url="http://127.0.0.1:30080/swarm-lab/target-scratch",
        work_branch="agent/vertex-29",
    )


def test_factory_builds_executor_with_handle_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import agent_runtime.kube_sandbox as ks

    created: dict[str, Any] = {}

    class SpyExecutor(OpencodeExecutor):
        def __init__(self, **kwargs: Any) -> None:
            created.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(ks, "OpencodeExecutor", SpyExecutor)
    kube = FakeKube(
        [
            (
                "get svc",
                subprocess.CompletedProcess([], 0, stdout=json.dumps({"items": []}), stderr=""),
            ),
            ("get pod", subprocess.CompletedProcess([], 0, stdout=_ready_pod_json(), stderr="")),
            (
                "kubectl -n agents logs",
                subprocess.CompletedProcess(
                    [], 0, stdout="server password pod-secret-token\n", stderr=""
                ),
            ),
        ]
    )
    factory = ks.KubeSandboxExecutorFactory(
        gitea_token="gitea-admin-TOKEN-SECRET",
        litellm_api_key="sk-master-KEY-SECRET",
        litellm_base_url="http://litellm.platform.svc.cluster.local:4000",
        model="litellm/qwen3",
        evidence_root=tmp_path,
        kube_runner=kube,
        transport=httpx.MockTransport(_opencode_mock),
    )
    session = asyncio.run(factory.start(_factory_request()))
    assert created["base_url"] == "http://127.0.0.1:30100"
    assert created["auth"] == ("opencode", "pod-secret-token")
    assert created["evidence_root"] == tmp_path
    assert created["model"] == "litellm/qwen3"
    assert isinstance(session, ks.SandboxSession)


def test_sandbox_session_stop_tears_down_sandbox() -> None:
    import agent_runtime.kube_sandbox as ks

    kube = FakeKube(
        [
            ("get pod", subprocess.CompletedProcess([], 0, stdout=_ready_pod_json(), stderr="")),
            (
                "kubectl -n agents logs",
                subprocess.CompletedProcess([], 0, stdout="server password tok\n", stderr=""),
            ),
        ]
    )
    factory = ks.KubeSandboxExecutorFactory(
        gitea_token="t",
        litellm_api_key="k",
        litellm_base_url="http://litellm.platform.svc.cluster.local:4000",
        model="litellm/qwen3",
        evidence_root=Path("/tmp/ev"),
        kube_runner=kube,
        transport=httpx.MockTransport(_opencode_mock),
    )

    async def drive() -> None:
        session = await factory.start(_factory_request())
        assert isinstance(session, ks.SandboxSession)
        await session.stop()

    asyncio.run(drive())
    deletes = " ".join(" ".join(argv) for argv, _ in kube.calls_with("delete"))
    assert "pod/agent-30-29" in deletes
    assert "secret/agent-30-29" in deletes
    assert "service/agent-30-29" in deletes


def test_factory_start_sandbox_failure_propagates() -> None:
    import agent_runtime.kube_sandbox as ks

    kube = FakeKube(
        [("apply Secret", subprocess.CompletedProcess([], 1, stdout="", stderr="boom"))]
    )
    factory = ks.KubeSandboxExecutorFactory(
        gitea_token="t",
        litellm_api_key="k",
        litellm_base_url="http://litellm.platform.svc.cluster.local:4000",
        model="litellm/qwen3",
        evidence_root=Path("/tmp/ev"),
        kube_runner=kube,
        transport=httpx.MockTransport(_opencode_mock),
    )
    with pytest.raises(KubeSandboxError):
        asyncio.run(factory.start(_factory_request()))


def test_clone_script_keeps_token_out_of_url_and_argv() -> None:
    """The agent-readable workspace must never carry the Gitea token."""
    pod = pod_manifest(
        image="opencode-git:2.2.0",
        repo_url="http://127.0.0.1:30080/swarm-lab/target-scratch",
        base_branch="main",
        run_id=30,
        vertex_id=29,
        secret_name="agent-30-29",
        config_jsonc="{}\n",
    )
    clone_script = " ".join(pod["spec"]["initContainers"][0]["command"])
    assert "oauth2:" not in clone_script, "token must not travel in the clone URL"
    assert "$GITEA_TOKEN" in clone_script, "the token is expanded at runtime from env"
    assert "GIT_CONFIG_KEY_0=http.extraHeader" in clone_script, (
        "credentials go through git env-based config, like the worker's gitops"
    )
    manifest_text = json.dumps(pod)
    assert "gitea-admin-TOKEN-SECRET" not in manifest_text, (
        "the real token value must never be baked into the pod manifest"
    )


def test_reconcile_orphans_cleans_service_pod_secret() -> None:
    kube = FakeKube(
        [
            (
                "get pods",
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=_pod_list_json(
                        [
                            {
                                "metadata": {
                                    "name": "agent-1-1",
                                    "labels": {"agent-runtime.io/managed": "yes"},
                                    "creationTimestamp": "2026-11-09T00:00:00Z",
                                }
                            }
                        ]
                    ),
                    stderr="",
                ),
            )
        ]
    )
    removed = reconcile_orphans(kube, namespace="agents", max_age_seconds=3600.0)
    assert removed == 1
    targets = " ".join(" ".join(argv) for argv, _ in kube.calls_with("delete"))
    for obj in ("service/agent-1-1", "pod/agent-1-1", "secret/agent-1-1"):
        assert obj in targets, f"{obj} must be reclaimed with the pod (credentials!)"


def test_reconcile_orphans_uses_creation_timestamp_for_pending_pods() -> None:
    """Pending pods have no status.startTime — they must still be reclaimed."""
    kube = FakeKube(
        [
            (
                "get pods",
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=_pod_list_json(
                        [
                            {
                                "metadata": {
                                    "name": "agent-2-2",
                                    "labels": {"agent-runtime.io/managed": "yes"},
                                    "creationTimestamp": "2026-11-09T00:00:00Z",
                                },
                                "status": {"phase": "Pending"},
                            }
                        ]
                    ),
                    stderr="",
                ),
            )
        ]
    )
    assert reconcile_orphans(kube, namespace="agents", max_age_seconds=3600.0) == 1
    assert kube.calls_with("delete", "pod/agent-2-2")


def test_malformed_pod_status_tears_down() -> None:
    kube = FakeKube(
        [
            ("get pod", subprocess.CompletedProcess([], 0, stdout="NOT-JSON{{", stderr="")),
        ]
    )
    sandbox = KubeSandbox(kube_runner=kube, **_sandbox_args())
    with pytest.raises((KubeSandboxError, json.JSONDecodeError)):
        sandbox.start(timeout_seconds=5.0)
    targets = " ".join(" ".join(argv) for argv, _ in kube.calls_with("delete"))
    for obj in ("service/agent-30-29", "pod/agent-30-29", "secret/agent-30-29"):
        assert obj in targets, f"start failure at any stage must tear down {obj}"


def test_teardown_reports_all_delete_failures() -> None:
    kube = FakeKube(
        [
            ("get pod", subprocess.CompletedProcess([], 0, stdout=_ready_pod_json(), stderr="")),
            (
                "kubectl -n agents logs",
                subprocess.CompletedProcess([], 0, stdout="server password tok\n", stderr=""),
            ),
            ("delete service", subprocess.CompletedProcess([], 1, stdout="", stderr="e-svc")),
            ("delete pod", subprocess.CompletedProcess([], 1, stdout="", stderr="e-pod")),
            ("delete secret", subprocess.CompletedProcess([], 1, stdout="", stderr="e-secret")),
        ]
    )
    sandbox = KubeSandbox(kube_runner=kube, **_sandbox_args())
    sandbox.start(timeout_seconds=5.0)
    with pytest.raises(KubeSandboxError) as excinfo:
        sandbox.stop()
    message = str(excinfo.value)
    for detail in ("service", "pod", "secret"):
        assert detail in message, f"teardown must report the failed {kind_name(detail)} deletion"


def kind_name(detail: str) -> str:
    return detail


def test_sandbox_session_stop_tears_down_even_if_inner_stop_raises() -> None:
    import agent_runtime.kube_sandbox as ks

    kube = FakeKube(
        [
            ("get pod", subprocess.CompletedProcess([], 0, stdout=_ready_pod_json(), stderr="")),
            (
                "kubectl -n agents logs",
                subprocess.CompletedProcess([], 0, stdout="server password tok\n", stderr=""),
            ),
        ]
    )
    factory = ks.KubeSandboxExecutorFactory(
        gitea_token="t",
        litellm_api_key="k",
        litellm_base_url="http://litellm.platform.svc.cluster.local:4000",
        model="litellm/qwen3",
        evidence_root=Path("/tmp/ev"),
        kube_runner=kube,
        transport=httpx.MockTransport(_opencode_mock),
    )

    async def drive() -> None:
        session = await factory.start(_factory_request())
        assert isinstance(session, ks.SandboxSession)
        assert session._inner is not None

        async def broken_stop() -> None:
            raise RuntimeError("inner stop boom")

        session._inner.stop = broken_stop  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="inner stop boom"):
            await session.stop()

    asyncio.run(drive())
    assert kube.calls_with("delete", "pod/agent-30-29"), (
        "sandbox must be stopped despite inner failure"
    )


def test_manifests_honor_custom_namespace() -> None:
    pod = pod_manifest(
        image="opencode-git:2.2.0",
        repo_url="http://127.0.0.1:30080/swarm-lab/target-scratch",
        base_branch="main",
        run_id=30,
        vertex_id=29,
        secret_name="agent-30-29",
        config_jsonc="{}\n",
        namespace="agents-x",
    )
    secret = secret_manifest(
        name="agent-30-29",
        run_id=30,
        vertex_id=29,
        gitea_token="t",
        litellm_api_key="k",
        config_jsonc="{}\n",
        namespace="agents-x",
    )
    svc = service_manifest(
        name="agent-30-29", run_id=30, vertex_id=29, node_port=30100, namespace="agents-x"
    )
    assert pod["metadata"]["namespace"] == "agents-x"
    assert secret["metadata"]["namespace"] == "agents-x"
    assert svc["metadata"]["namespace"] == "agents-x"
