"""z.ai-first gate: OpenRouter only after z.ai reports its plan limit (owner directive).

LiteLLM's generic `fallbacks` fire on ANY error; the gate (a LiteLLM proxy
callback) disables them per request until z.ai answers HTTP 429.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "infra" / "litellm" / "zai_limit_gate.py"
ZAI = "https://api.z.ai/api/coding/paas/v4"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("zai_limit_gate", GATE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Err(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.status_code = status


def _failure(status: int, api_base: str = ZAI) -> dict[str, Any]:
    return {"exception": _Err(status), "litellm_params": {"api_base": api_base}}


def _gate(now: list[float]) -> Any:
    return _module().ZaiLimitGate(cooldown_s=300, clock=lambda: now[0])


def _fail(gate: Any, kwargs: dict[str, Any]) -> None:
    asyncio.run(gate.async_log_failure_event(kwargs, None, 0, 0))


def _pre(gate: Any, model: str) -> dict[str, Any]:
    data = {"model": model, "messages": []}
    out: dict[str, Any] = asyncio.run(gate.async_pre_call_hook(None, None, data, "completion"))
    return out


def test_fallbacks_disabled_while_zai_has_quota() -> None:
    gate = _gate([0.0])
    for model in ("qwen3", "qwen-coder", "reviewer"):
        assert _pre(gate, model)["disable_fallbacks"] is True, model


def test_zai_429_opens_openrouter_for_the_cooldown() -> None:
    now = [0.0]
    gate = _gate(now)
    _fail(gate, _failure(429))
    assert "disable_fallbacks" not in _pre(gate, "qwen3")
    now[0] = 299.0
    assert "disable_fallbacks" not in _pre(gate, "qwen-coder")
    now[0] = 301.0
    assert _pre(gate, "qwen3")["disable_fallbacks"] is True


def test_non_limit_zai_errors_never_open_openrouter() -> None:
    gate = _gate([0.0])
    for status in (400, 401, 404, 408, 500, 502, 503):
        _fail(gate, _failure(status))
    assert _pre(gate, "qwen3")["disable_fallbacks"] is True


def test_openrouter_429_does_not_count_as_zai_limit() -> None:
    gate = _gate([0.0])
    _fail(gate, _failure(429, "https://openrouter.ai/api/v1"))
    assert _pre(gate, "qwen3")["disable_fallbacks"] is True


def test_rate_limit_error_class_without_status_counts() -> None:
    class RateLimitError(Exception):
        pass

    gate = _gate([0.0])
    _fail(gate, {"exception": RateLimitError("limit"), "litellm_params": {"api_base": ZAI}})
    assert "disable_fallbacks" not in _pre(gate, "qwen3")


def test_api_base_from_standard_logging_object() -> None:
    gate = _gate([0.0])
    _fail(gate, {"exception": _Err(429), "standard_logging_object": {"api_base": ZAI}})
    assert "disable_fallbacks" not in _pre(gate, "qwen3")


def test_other_models_untouched_and_garbage_ignored() -> None:
    gate = _gate([0.0])
    assert "disable_fallbacks" not in _pre(gate, "qwen3-openrouter")
    _fail(gate, {})
    _fail(gate, {"exception": None, "litellm_params": None})
    assert _pre(gate, "qwen3")["disable_fallbacks"] is True


def test_module_exports_proxy_instance() -> None:
    mod = _module()
    assert isinstance(mod.gate, mod.ZaiLimitGate)
