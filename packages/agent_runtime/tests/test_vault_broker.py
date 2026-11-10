"""Tests for the Vault-brokered read-only Gitea token."""

from __future__ import annotations

import json
import subprocess

import pytest
from agent_runtime.vault_broker import VaultBrokerError, read_gitea_token

_ROOT = "vault-root-token-SECRET"
_GITEA = "gitea-admin-TOKEN-SECRET"


def _pod_list_json() -> str:
    return json.dumps({"items": [{"metadata": {"name": "vault-openbao-0"}}]})


def test_read_gitea_token_returns_field() -> None:
    calls: list[tuple[list[str], str | None]] = []

    def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        calls.append((argv, stdin))
        if "get" in argv and "pods" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=_pod_list_json(), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout=_GITEA + "\n", stderr="")

    token = read_gitea_token(run, root_token=_ROOT)
    assert token == _GITEA
    exec_calls = [c for c in calls if "exec" in c[0]]
    assert len(exec_calls) == 1
    argv_text = " ".join(exec_calls[0][0])
    assert "bao kv get -field=token secret/gitea/admin" in argv_text
    assert "BAO_ADDR=http://127.0.0.1:8200" in argv_text
    # The returned gitea token must never travel through argv.
    assert _GITEA not in argv_text
    # The root token reaches bao through the exec env (m2a precedent).
    assert f"BAO_TOKEN={_ROOT}" in argv_text


def test_token_not_in_exception_on_failure() -> None:
    def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        if "get" in argv and "pods" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=_pod_list_json(), stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="permission denied")

    with pytest.raises(VaultBrokerError) as excinfo:
        read_gitea_token(run, root_token=_ROOT)
    assert _GITEA not in str(excinfo.value)


def test_no_pod_found_raises() -> None:
    def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"items": []}), stderr="")

    with pytest.raises(VaultBrokerError):
        read_gitea_token(run, root_token=_ROOT)
