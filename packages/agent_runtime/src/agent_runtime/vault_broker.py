"""Vault brokering: Gitea credentials for sandbox pods, via kubectl exec.

v0 reads the platform's ADMIN Gitea token (owner-approved builder-check
exception to the "read-only sandbox credentials" goal; per-project read-only
tokens are the M4c/M5 hardening line). The root token travels through the
runner stdin — never argv — and the RETURNED token never appears in argv or
exception text.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

KubeRunner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


class VaultBrokerError(RuntimeError):
    """Raised when a secret cannot be read from OpenBao."""


def read_gitea_token(
    kubectl_runner: KubeRunner,
    *,
    root_token: str,
    namespace: str = "platform",
    pod_label: str = "app.kubernetes.io/name=openbao",
) -> str:
    """Return the Gitea token stored at secret/gitea/admin (field: token)."""
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
        "BAO_ADDR=http://127.0.0.1:8200 bao kv get -field=token secret/gitea/admin"
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
            f"bao kv get failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    token = result.stdout.strip()
    if not token:
        raise VaultBrokerError("bao kv get returned an empty token")
    return token
