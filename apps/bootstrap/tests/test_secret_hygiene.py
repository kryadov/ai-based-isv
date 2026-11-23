"""M12: secrets never reach a process argv in the bootstrap sections that write
k8s Secrets (``ps`` on the stand host would show them), and the host-facing
task-source MCP gets its bearer token from OpenBao."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = ROOT / "infra" / "scripts" / "bootstrap.sh"


def _section(name: str) -> str:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    body = text[text.index(f"{name}() {{") :]
    return body[: body.index("\n}\n")]


def test_secret_sections_never_pass_values_as_from_literal() -> None:
    for name in ("section_mcp", "section_litellm", "section_platform"):
        body = _section(name)
        assert not re.search(r"--from-literal=\"?[\w-]+=\$", body), name
        assert "apply_secret" in body, name


def test_apply_secret_renders_through_builtins_into_stdin() -> None:
    body = _section("apply_secret")
    assert "printf" in body
    assert "kubectl" in body and "apply -f -" in body
    assert "--from-literal" not in body
    assert "base64" in body


def test_section_mcp_creates_and_wires_the_task_source_token() -> None:
    body = _section("section_mcp")
    assert "secret/mcp/task-source" in body
    assert "auth-token" in body
    # the probe proves both sides: refused without the token, served with it
    assert "401" in body


def test_task_source_deployment_reads_mcp_auth_token_from_its_secret() -> None:
    docs = list(yaml.safe_load_all((ROOT / "infra/mcp/task-source.yaml").read_text("utf-8")))
    deploy = next(d for d in docs if d and d["kind"] == "Deployment")
    env = {e["name"]: e for e in deploy["spec"]["template"]["spec"]["containers"][0]["env"]}
    ref = env["MCP_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "mcp-task-source", "key": "auth-token"}


def test_no_secret_reaches_argv_anywhere_in_bootstrap() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "--from-literal" not in text
    # credentials in curl argv: headers, basic auth, request bodies with passwords
    assert not re.search(r"\bcurl\b[^\n]*-H \"Authorization", text)
    assert not re.search(r"\bcurl\b[^\n]*-u \"", text)
    assert not re.search(r"-d \"[^\n]*password", text)
    # helm --set with a password; git -c with a token; bao token/values in exec argv
    assert not re.search(r"--set \"[^\n]*(password|PASSWD)", text)
    assert "http.extraHeader=Authorization" not in text
    assert 'BAO_TOKEN="$RTOK"' not in text
    assert not re.search(r"vcli kv put", text)
    assert not re.search(r"operator unseal \"\$", text)


def test_section_platform_writes_identity_tokens_and_only_hashes_reach_the_cluster() -> None:
    body = "".join(
        _section(n) for n in ("section_platform", "platform_api_tokens", "apply_agent_api_secret")
    )
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'PLATFORM_IDENTITIES="token daemon worker poller"' in text
    assert "platform_api_tokens" in body and "apply_agent_api_secret" in body
    assert "service-tokens=" in body
    assert "sha256sum" in body
    # no plaintext service token in the agent-api Secret any more
    assert '"service-token=' not in body


def test_platform_token_rotate_is_dispatchable() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert re.search(r"platform-token\)[^\n]*section_platform_token", text)
    body = _section("section_platform_token")
    assert "rotate" in body
    assert "rollout restart" in body
    assert "openssl rand" in _section("platform_api_tokens")


def test_section_platform_drops_a_merged_platform_live_branch() -> None:
    body = _section("drop_merged_platform_live")
    assert "merge-base --is-ancestor" in body
    assert "branches/platform-live" in body and "DELETE" in body
    assert "drop_merged_platform_live" in _section("section_platform")
