"""Vault brokering: Gitea credentials for sandbox pods, via kubectl exec.

Agents act as the NON-admin Gitea user ``swarm-agent`` (OpenBao
``secret/gitea/agent``, created by ``bootstrap.sh agent-user``): write on the
``swarm-lab`` org repos but no admin rights, so an agent cannot rewrite branch
protection or its own gates (self-dev level 2, design §3). There is no
fallback to the admin token: a missing secret fails closed. The root token
travels through the runner stdin — never argv — and the RETURNED token never
appears in argv or exception text.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

AGENT_TOKEN_PATH = "secret/gitea/agent"

KubeRunner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


class VaultBrokerError(RuntimeError):
    """Raised when a secret cannot be read from OpenBao."""


def read_gitea_token(
    kubectl_runner: KubeRunner,
    *,
    root_token: str,
    namespace: str = "platform",
    pod_label: str = "app.kubernetes.io/name=openbao",
    path: str = AGENT_TOKEN_PATH,
) -> str:
    """Return the agent Gitea token stored at ``path`` (field: token)."""
    listing = kubectl_runner(
        ["kubectl", "-n", namespace, "get", "pods", "-l", pod_label, "-o", "json"], None
    )
    if listing.returncode != 0:
        raise VaultBrokerError(f"could not list OpenBao pods: {listing.stderr.strip()}")
    items = json.loads(listing.stdout or "{}").get("items", [])
    if not items:
        raise VaultBrokerError(f"no OpenBao pod with label {pod_label!r} in {namespace}")
    pod_name = items[0]["metadata"]["name"]
    # The root token reaches the pod through stdin (a `read` in the remote
    # shell), never through argv — process listings must not see it.
    remote_script = (
        "BAO_TOKEN=$(head -c 128); export BAO_TOKEN; "
        "BAO_ADDR=http://127.0.0.1:8200 bao kv get -field=token " + path
    )
    result = kubectl_runner(
        [
            "kubectl",
            "-n",
            namespace,
            "exec",
            "-i",
            pod_name,
            "--",
            "sh",
            "-c",
            remote_script,
        ],
        root_token,
    )
    if result.returncode != 0:
        raise VaultBrokerError(
            f"bao kv get {path} failed (exit {result.returncode}): {result.stderr.strip()}"
            " — create the agent identity with: bootstrap.sh agent-user"
        )
    token = result.stdout.strip()
    if not token:
        raise VaultBrokerError("bao kv get returned an empty token")
    return token
