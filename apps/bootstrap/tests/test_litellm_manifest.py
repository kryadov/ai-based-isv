"""LiteLLM gateway manifest invariants: external API provider, key from a Secret."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "infra" / "litellm" / "deployment.yaml"
BOOTSTRAP = ROOT / "infra" / "scripts" / "bootstrap.sh"


def _docs() -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")) if d]


def _model_list() -> list[dict[str, Any]]:
    cm = next(d for d in _docs() if d["kind"] == "ConfigMap")
    config: dict[str, Any] = yaml.safe_load(cm["data"]["config.yaml"])
    models: list[dict[str, Any]] = config["model_list"]
    return models


def test_flow_aliases_still_served() -> None:
    # The acceptance flows and the llm MCP server address these aliases.
    names = {m["model_name"] for m in _model_list()}
    assert {"qwen3", "qwen-coder"} <= names


def test_models_route_to_openrouter_not_host_ollama() -> None:
    for m in _model_list():
        params = m["litellm_params"]
        assert params["model"].startswith("openrouter/"), m
        assert "api_base" not in params, m
        # Never a literal key in git: LiteLLM resolves it from the pod env.
        assert params["api_key"] == "os.environ/OPENROUTER_API_KEY", m


def test_provider_key_comes_from_secret() -> None:
    deploy = next(d for d in _docs() if d["kind"] == "Deployment")
    env = {e["name"]: e for e in deploy["spec"]["template"]["spec"]["containers"][0]["env"]}
    ref = env["OPENROUTER_API_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "litellm-provider", "key": "openrouter-api-key"}


def test_bootstrap_has_no_host_ollama_wiring() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "host.lima.internal" not in text
    assert "getent hosts" not in text  # no host-IP discovery for a host model server
    # The key lives in OpenBao and is synced into the Secret by section_litellm.
    assert "secret/litellm/openrouter" in text


def test_env_example_documents_the_key_without_a_value() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in example.splitlines()]
    assert "OPENROUTER_API_KEY=" in lines  # placeholder only, never a real value


def test_dotenv_is_gitignored_but_example_is_not() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore
    assert "!.env.example" in ignore


def test_bootstrap_imports_the_key_from_dotenv() -> None:
    # The owner keeps the key in the repo-root .env (gitignored); the
    # bootstrapper moves it into OpenBao when OpenBao has none yet.
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY" in text
    assert '/.env"' in text
