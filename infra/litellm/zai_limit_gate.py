"""LiteLLM proxy callback: OpenRouter ONLY after the z.ai plan limit is hit.

Owner directive (2026-11-15): requests go to the z.ai GLM Coding Plan; the
metered OpenRouter path is used only when the plan limit is exhausted.
LiteLLM's generic `fallbacks` fire on ANY error (5xx, timeout, auth...), so
this gate sets `disable_fallbacks` on every request to a z.ai-first alias
until z.ai itself answers HTTP 429 (plan/rate limit). From that moment the
fallbacks are allowed for `cooldown_s` — the same window the router parks the
z.ai deployment for (`cooldown_time` in deployment.yaml).

Loaded by the proxy from the config dir: `litellm_settings.callbacks:
zai_limit_gate.gate`. Kept free of hard litellm imports so it unit-tests
without the proxy (apps/bootstrap/tests/test_zai_limit_gate.py).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

try:  # inside the LiteLLM proxy image
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # unit tests: no litellm installed
    CustomLogger = object  # type: ignore[assignment,misc]

# Aliases whose primary deployment is z.ai (deployment.yaml model_list).
ZAI_FIRST_ALIASES = frozenset({"qwen3", "qwen-coder", "reviewer"})
ZAI_HOST = "api.z.ai"
COOLDOWN_S = 300.0


def _api_base(kwargs: Mapping[str, Any]) -> str:
    for key in ("litellm_params", "standard_logging_object"):
        section = kwargs.get(key)
        if isinstance(section, Mapping) and section.get("api_base"):
            return str(section["api_base"])
    return str(kwargs.get("api_base") or "")


def _is_limit(exc: object) -> bool:
    if exc is None:
        return False
    if getattr(exc, "status_code", None) == 429:
        return True
    return type(exc).__name__ == "RateLimitError"


class ZaiLimitGate(CustomLogger):  # type: ignore[misc,valid-type]
    def __init__(
        self, *, cooldown_s: float = COOLDOWN_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__()
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._limited_until = float("-inf")

    def zai_limited(self) -> bool:
        return self._clock() < self._limited_until

    async def async_log_failure_event(
        self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        if not isinstance(kwargs, Mapping):
            return
        if ZAI_HOST in _api_base(kwargs) and _is_limit(kwargs.get("exception")):
            self._limited_until = self._clock() + self._cooldown_s
            print(f"zai_limit_gate: z.ai limit hit; OpenRouter on for {self._cooldown_s:.0f}s")

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: Any
    ) -> dict[str, Any]:
        if data.get("model") in ZAI_FIRST_ALIASES and not self.zai_limited():
            data["disable_fallbacks"] = True
        return data


gate = ZaiLimitGate()
