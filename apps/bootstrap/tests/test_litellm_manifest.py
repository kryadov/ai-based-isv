"""LiteLLM gateway invariants: z.ai Coding Plan first, OpenRouter on limits.

Keys never live in git: `.env` (gitignored) → OpenBao → Secret → pod env.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "infra" / "litellm" / "deployment.yaml"
BOOTSTRAP = ROOT / "infra" / "scripts" / "bootstrap.sh"

ZAI_BASE = "https://api.z.ai/api/coding/paas/v4"
ALIASES = ("qwen3", "qwen-coder")


def _docs() -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")) if d]


def _config() -> dict[str, Any]:
    cm = next(d for d in _docs() if d["kind"] == "ConfigMap")
    config: dict[str, Any] = yaml.safe_load(cm["data"]["config.yaml"])
    return config


def _models() -> dict[str, dict[str, Any]]:
    return {m["model_name"]: m["litellm_params"] for m in _config()["model_list"]}


def test_flow_aliases_route_to_zai_coding_plan_first() -> None:
    # The acceptance flows and the llm MCP server address these aliases.
    models = _models()
    for alias in ALIASES:
        params = models[alias]
        assert params["api_base"] == ZAI_BASE, alias
        assert params["model"].startswith("openai/glm-"), alias
        assert params["api_key"] == "os.environ/ZAI_API_KEY", alias


def test_each_alias_falls_back_to_openrouter() -> None:
    models = _models()
    fallbacks: dict[str, list[str]] = {}
    for entry in _config()["router_settings"]["fallbacks"]:
        fallbacks.update(entry)
    for alias in ALIASES:
        targets = fallbacks[alias]
        assert targets, alias
        for target in targets:
            params = models[target]
            assert params["model"].startswith("openrouter/"), target
            assert "api_base" not in params, target
            assert params["api_key"] == "os.environ/OPENROUTER_API_KEY", target


def test_rate_limited_deployment_cools_down() -> None:
    # After z.ai reports its plan limit, skip it for a while instead of
    # paying a failed round-trip on every request.
    router = _config()["router_settings"]
    assert router["allowed_fails"] >= 1
    assert router["cooldown_time"] >= 60


def test_provider_keys_come_from_an_optional_secret() -> None:
    deploy = next(d for d in _docs() if d["kind"] == "Deployment")
    env = {e["name"]: e for e in deploy["spec"]["template"]["spec"]["containers"][0]["env"]}
    for var, key in (("ZAI_API_KEY", "zai-api-key"), ("OPENROUTER_API_KEY", "openrouter-api-key")):
        ref = env[var]["valueFrom"]["secretKeyRef"]
        # Either provider may be absent; the pod must still start.
        assert ref == {"name": "litellm-provider", "key": key, "optional": True}


def test_bootstrap_has_no_host_ollama_wiring() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "host.lima.internal" not in text
    assert "getent hosts" not in text  # no host-IP discovery for a host model server


def test_bootstrap_syncs_both_keys_through_openbao() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    for path, var in (
        ("secret/litellm/zai", "ZAI_API_KEY"),
        ("secret/litellm/openrouter", "OPENROUTER_API_KEY"),
    ):
        assert path in text
        assert var in text
    assert '/.env"' in text


def test_env_example_documents_the_keys_without_values() -> None:
    lines = [ln.strip() for ln in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()]
    assert "ZAI_API_KEY=" in lines  # placeholders only, never a real value
    assert "OPENROUTER_API_KEY=" in lines


def test_dotenv_is_gitignored_but_example_is_not() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore
    assert "!.env.example" in ignore


def test_light_alias_disables_reasoning() -> None:
    # glm-5.3-flash thinks by default: a short completion (m2c gateway check,
    # max_tokens=16) is spent entirely on reasoning and comes back empty.
    params = _models()["qwen-coder"]
    assert params["extra_body"]["thinking"] == {"type": "disabled"}


def test_bootstrap_waits_for_the_new_litellm_rollout() -> None:
    # `wait --for=condition=Available` right after `rollout restart` is
    # satisfied by the OLD pod; the readiness ping then hits it and the gateway
    # vanishes seconds later while the new pod starts (m2c/m4c 2026-11-15).
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "rollout status deploy/litellm" in text
    assert "wait --for=condition=Available deployment/litellm" not in text
