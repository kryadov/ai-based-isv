"""Bot review of a PR diff via the LiteLLM gateway (M5, spec §7.2 item 4).

The reviewer model belongs to a different family than the implementer
(LiteLLM alias ``reviewer``). Output is structured JSON validated by pydantic;
anything else gets exactly one retry, then :class:`ReviewUnavailable` — the
gate corridor treats that as "do not merge", never as a pass.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

PROMPT_VERSION = "reviewer-v1"
DEFAULT_BUDGET_CHARS = 60000
_REDACTED = "***"


class Finding(BaseModel):
    path: str
    line: int
    severity: Literal["blocking", "nit"]
    body: str


class _ReviewPayload(BaseModel):
    summary: str
    findings: list[Finding]


class Review(BaseModel):
    summary: str
    findings: list[Finding]
    model: str
    prompt_version: str
    truncated: bool

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]


class ReviewUnavailable(RuntimeError):
    """The reviewer could not produce a valid review; the merge must wait."""


def _prompt() -> str:
    return (
        resources.files("agent_runtime")
        .joinpath(f"prompts/{PROMPT_VERSION}.md")
        .read_text(encoding="utf-8")
    )


def truncate_diff(diff: str, budget_chars: int) -> tuple[str, bool]:
    """Cut ``diff`` to ``budget_chars`` at a hunk/file boundary; True if cut."""
    if len(diff) <= budget_chars:
        return diff, False
    head = diff[:budget_chars]
    cut = max(head.rfind("\n@@ "), head.rfind("\ndiff --git "))
    return (head[: cut + 1] if cut > 0 else head), True


async def review_diff(
    http: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model: str,
    goal: str,
    diff: str,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
) -> Review:
    """Review ``diff`` against ``goal``; raise ReviewUnavailable on any failure."""
    body_diff, truncated = truncate_diff(diff, budget_chars)
    note = "\n\n(The diff was truncated to the review budget; say so in the summary.)"
    messages = [
        {"role": "system", "content": _prompt()},
        {
            "role": "user",
            "content": f"Task goal:\n{goal}\n\nDiff:\n{body_diff}" + (note if truncated else ""),
        },
    ]
    last_error = "no attempt"
    for _attempt in range(2):
        try:
            response = await http.post(
                f"{base_url.rstrip('/')}/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                },
                timeout=300.0,
            )
        except httpx.HTTPError as exc:
            last_error = _redact(f"reviewer call failed: {exc}", api_key)
            continue
        if response.status_code >= 400:
            last_error = f"reviewer HTTP {response.status_code}"
            continue
        try:
            content = response.json()["choices"][0]["message"]["content"]
            payload = _ReviewPayload.model_validate(json.loads(content))
        except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
            last_error = f"invalid reviewer output: {type(exc).__name__}"
            continue
        return Review(
            summary=payload.summary,
            findings=payload.findings,
            model=model,
            prompt_version=PROMPT_VERSION,
            truncated=truncated,
        )
    raise ReviewUnavailable(_redact(last_error, api_key))


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, _REDACTED) if secret else text
