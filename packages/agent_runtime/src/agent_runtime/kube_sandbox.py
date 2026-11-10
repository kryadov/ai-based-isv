"""k3s sandbox lifecycle: pod/service/secret over a kubectl runner seam.

The sandbox replaces the M4a docker spawn without touching the Executor
contract: the worker attaches `OpencodeExecutor` to `SandboxHandle.base_url`
(the adapter's attached-mode seam). Evidence stays host-side — the pod holds
only the cloned repo, the generated opencode.jsonc, and OPENCODE_DB.

Secret material never travels in argv: the Gitea token and the harness
config live in a per-run Secret applied via stdin; the initContainer reads
them as env/volume. Teardown deletes Pod, Service and Secret (idempotent,
`--ignore-not-found`); the PVC persists.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

from agent_runtime.opencode_executor import opencode_config_text
from agent_runtime.settings import OPENCODE_IMAGE

#: Same shape as the docker runner seam (argv, stdin) — keeps the injection
#: test pattern identical across sandbox backends.
KubeRunner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]

IN_CLUSTER_GITEA_HOST = "gitea-http.platform.svc.cluster.local:3000"
_LABEL_MANAGED = "agent-runtime.io/managed"
_LABEL_RUN = "agent-runtime.io/run-id"
_LABEL_VERTEX = "agent-runtime.io/vertex-id"
_PASSWORD_PREFIX = "server password "


class KubeSandboxError(RuntimeError):
    """Raised when the sandbox cannot be created or torn down."""


def _kubectl_runner(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, input=stdin)


def in_cluster_clone_url(repo_url: str) -> str:
    """Rewrite a host-reachable Gitea URL to the in-cluster service endpoint."""
    parts = urlsplit(repo_url)
    return urlunsplit(("http", IN_CLUSTER_GITEA_HOST, parts.path, "", ""))


def _object_name(run_id: int, vertex_id: int) -> str:
    return f"agent-{run_id}-{vertex_id}"


def _labels(run_id: int, vertex_id: int) -> dict[str, str]:
    return {
        _LABEL_MANAGED: "yes",
        _LABEL_RUN: str(run_id),
        _LABEL_VERTEX: str(vertex_id),
    }


def secret_manifest(
    *,
    name: str,
    run_id: int,
    vertex_id: int,
    gitea_token: str,
    litellm_api_key: str,
    config_jsonc: str,
) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "namespace": "agents",
            "labels": _labels(run_id, vertex_id),
        },
        "stringData": {
            "gitea-token": gitea_token,
            "opencode.jsonc": config_jsonc,
            "litellm-api-key": litellm_api_key,
        },
    }


def pod_manifest(
    *,
    image: str,
    repo_url: str,
    base_branch: str,
    run_id: int,
    vertex_id: int,
    secret_name: str,
    config_jsonc: str,
) -> dict[str, Any]:
    name = _object_name(run_id, vertex_id)
    run_dir = f"/workspace/run-{run_id}/vertex-{vertex_id}"
    clone_url = in_cluster_clone_url(repo_url)
    # The token is substituted from the GITEA_TOKEN env (Secret-backed) at
    # execution time — the manifest itself carries no secret material.
    credentialed_url = clone_url.replace("http://", "http://oauth2:$GITEA_TOKEN@")
    clone_script = (
        "set -e; "
        f"mkdir -p '{run_dir}'; "
        f"git clone --depth 1 -b '{base_branch}' \"{credentialed_url}\" '{run_dir}'; "
        f"cp /config/opencode.jsonc '{run_dir}/opencode.jsonc'"
    )
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": "agents", "labels": _labels(run_id, vertex_id)},
        "spec": {
            "restartPolicy": "Never",
            "volumes": [
                {"name": "workspace", "persistentVolumeClaim": {"claimName": "agent-scratch"}},
                {"name": "config", "secret": {"secretName": secret_name}},
            ],
            "initContainers": [
                {
                    "name": "clone",
                    "image": "alpine/git:latest",
                    "command": ["sh", "-c", clone_script],
                    "env": [
                        {
                            "name": "GITEA_TOKEN",
                            "valueFrom": {
                                "secretKeyRef": {"name": secret_name, "key": "gitea-token"}
                            },
                        },
                    ],
                    "volumeMounts": [
                        {"name": "workspace", "mountPath": "/workspace"},
                        {"name": "config", "mountPath": "/config", "readOnly": True},
                    ],
                }
            ],
            "containers": [
                {
                    "name": "serve",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"],
                    "workingDir": run_dir,
                    "env": [
                        {
                            "name": "OPENCODE_DB",
                            "value": f"/workspace/run-{run_id}/.opencode-db-{vertex_id}",
                        },
                        {
                            "name": "LITELLM_API_KEY",
                            "valueFrom": {
                                "secretKeyRef": {"name": secret_name, "key": "litellm-api-key"}
                            },
                        },
                    ],
                    "ports": [{"containerPort": 4096}],
                }
            ],
        },
    }


def service_manifest(*, name: str, run_id: int, vertex_id: int, node_port: int) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": "agents",
            "labels": _labels(run_id, vertex_id),
        },
        "spec": {
            "type": "NodePort",
            "selector": {
                _LABEL_RUN: str(run_id),
                _LABEL_VERTEX: str(vertex_id),
            },
            "ports": [{"port": 4096, "targetPort": 4096, "nodePort": node_port}],
        },
    }


def _used_node_ports(kube: KubeRunner) -> set[int]:
    result = kube(["kubectl", "get", "svc", "-A", "-o", "json"], None)
    if result.returncode != 0:
        raise KubeSandboxError(f"could not list services: {result.stderr.strip()}")
    services = json.loads(result.stdout or "{}")
    used: set[int] = set()
    for item in services.get("items", []):
        for port in item.get("spec", {}).get("ports", []) or []:
            node_port = port.get("nodePort")
            if node_port is not None:
                used.add(int(node_port))
    return used


def _extract_password(logs: str) -> str | None:
    for line in logs.splitlines():
        if _PASSWORD_PREFIX in line:
            return line.split(_PASSWORD_PREFIX, 1)[1].strip()
    return None


class SandboxHandle(BaseModel):
    """Everything the worker needs to attach to (and tear down) a sandbox."""

    base_url: str
    password: str
    workspace_pod_path: str
    pod_name: str


class KubeSandbox:
    """One agent sandbox: a serve pod behind a NodePort, torn down on stop()."""

    def __init__(
        self,
        *,
        gitea_token: str,
        repo_url: str,
        base_branch: str,
        run_id: int,
        vertex_id: int,
        litellm_base_url: str,
        litellm_api_key: str,
        model: str,
        namespace: str = "agents",
        nodeport_range: tuple[int, int] = (30100, 30110),
        kube_runner: KubeRunner | None = None,
        image: str = OPENCODE_IMAGE,
    ) -> None:
        self._gitea_token = gitea_token
        self._repo_url = repo_url
        self._base_branch = base_branch
        self._run_id = run_id
        self._vertex_id = vertex_id
        self._litellm_base_url = litellm_base_url
        self._litellm_api_key = litellm_api_key
        self._model = model
        self._namespace = namespace
        self._low, self._high = nodeport_range
        self._kube = kube_runner if kube_runner is not None else _kubectl_runner
        self._image = image
        self._name = _object_name(run_id, vertex_id)

    # -- helpers ------------------------------------------------------------

    def _apply(self, manifest: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        return self._kube(
            ["kubectl", "apply", "-f", "-"],
            json.dumps(manifest),
        )

    def _fail(self, step: str, result: subprocess.CompletedProcess[str]) -> KubeSandboxError:
        return KubeSandboxError(
            f"`{' '.join(result.args)}` ({step}) failed "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )

    def _delete(self, kind: str) -> None:
        self._kube(
            [
                "kubectl",
                "-n",
                self._namespace,
                "delete",
                f"{kind}/{self._name}",
                "--ignore-not-found",
            ],
            None,
        )

    def teardown(self) -> None:
        """Idempotently remove Service, Pod and Secret (PVC persists)."""
        self._delete("service")
        self._delete("pod")
        self._delete("secret")

    # -- lifecycle ----------------------------------------------------------

    def start(self, timeout_seconds: float = 300.0) -> SandboxHandle:
        used = _used_node_ports(self._kube)
        free = [p for p in range(self._low, self._high + 1) if p not in used]
        if not free:
            raise KubeSandboxError(f"no free NodePort in range {self._low}-{self._high}")

        config_jsonc = opencode_config_text(model=self._model, llm_endpoint=self._litellm_base_url)
        secret = secret_manifest(
            name=self._name,
            run_id=self._run_id,
            vertex_id=self._vertex_id,
            gitea_token=self._gitea_token,
            litellm_api_key=self._litellm_api_key,
            config_jsonc=config_jsonc,
        )
        pod = pod_manifest(
            image=self._image,
            repo_url=self._repo_url,
            base_branch=self._base_branch,
            run_id=self._run_id,
            vertex_id=self._vertex_id,
            secret_name=self._name,
            config_jsonc=config_jsonc,
        )
        try:
            applied = self._apply(secret)
            if applied.returncode != 0:
                raise self._fail("secret", applied)
            pod_applied = self._apply(pod)
            if pod_applied.returncode != 0:
                raise self._fail("pod", pod_applied)
            node_port = self._apply_service_with_retry(free)
        except KubeSandboxError:
            self.teardown()
            raise

        deadline = time.monotonic() + timeout_seconds
        while True:
            probe = self._kube(
                ["kubectl", "-n", self._namespace, "get", "pod", self._name, "-o", "json"], None
            )
            if probe.returncode != 0:
                self.teardown()
                raise self._fail("pod probe", probe)
            status = json.loads(probe.stdout or "{}").get("status", {})
            if status.get("phase") == "Failed":
                self.teardown()
                raise KubeSandboxError(f"pod {self._name} entered phase Failed")
            if any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in status.get("conditions", [])
            ):
                break
            if time.monotonic() >= deadline:
                self.teardown()
                raise KubeSandboxError(f"pod {self._name} not Ready within {timeout_seconds}s")
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))

        logs = self._kube(["kubectl", "-n", self._namespace, "logs", self._name], None)
        if logs.returncode != 0:
            self.teardown()
            raise self._fail("logs", logs)
        password = _extract_password(logs.stdout)
        if password is None:
            self.teardown()
            raise KubeSandboxError(f"no 'server password' line in logs of {self._name}")
        return SandboxHandle(
            base_url=f"http://127.0.0.1:{node_port}",
            password=password,
            workspace_pod_path=f"/workspace/run-{self._run_id}/vertex-{self._vertex_id}",
            pod_name=self._name,
        )

    def stop(self) -> None:
        self.teardown()

    def _apply_service_with_retry(self, free: list[int]) -> int:
        last: subprocess.CompletedProcess[str] | None = None
        for port in free:
            manifest = service_manifest(
                name=self._name, run_id=self._run_id, vertex_id=self._vertex_id, node_port=port
            )
            result = self._apply(manifest)
            if result.returncode == 0:
                return port
            last = result
            if "already allocated" not in (result.stderr + result.stdout):
                break
        assert last is not None
        raise self._fail("service", last)


def reconcile_orphans(kube: KubeRunner, *, namespace: str, max_age_seconds: float) -> int:
    """Delete sandbox pods left behind by dead workers (older than max_age)."""
    result = kube(
        ["kubectl", "-n", namespace, "get", "pods", "-l", f"{_LABEL_MANAGED}=yes", "-o", "json"],
        None,
    )
    if result.returncode != 0:
        raise KubeSandboxError(f"could not list sandbox pods: {result.stderr.strip()}")
    now = datetime.now(UTC)
    removed = 0
    for item in json.loads(result.stdout or "{}").get("items", []):
        name = item.get("metadata", {}).get("name")
        started = item.get("status", {}).get("startTime")
        if not name or not started:
            continue
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if (now - start).total_seconds() > max_age_seconds:
            kube(
                ["kubectl", "-n", namespace, "delete", "pod", name, "--ignore-not-found"],
                None,
            )
            removed += 1
    return removed
