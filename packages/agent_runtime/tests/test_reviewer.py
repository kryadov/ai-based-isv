"""Cross-family LLM reviewer with structured findings (M5 Task 4)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from agent_runtime.reviewer import ReviewUnavailable, review_diff

KEY = "sk-lab-SECRET-key"
GOOD = {
    "summary": "One bug.",
    "findings": [
        {"path": "src/a.py", "line": 3, "severity": "blocking", "body": "off by one"},
        {"path": "src/a.py", "line": 9, "severity": "nit", "body": "naming"},
    ],
}


class FakeLLM:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        reply = self.replies[min(len(self.requests), len(self.replies)) - 1]
        if isinstance(reply, int):
            return httpx.Response(reply, text=f"upstream error {KEY}")
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def _review(llm: FakeLLM, diff: str = "diff --git a/x b/x\n+x\n") -> Any:
    http = httpx.AsyncClient(transport=httpx.MockTransport(llm.handler))
    return await review_diff(
        http, base_url="http://litellm", api_key=KEY, model="reviewer", goal="add x", diff=diff
    )


async def test_review_parses_findings() -> None:
    review = await _review(FakeLLM([GOOD]))
    assert review.summary == "One bug."
    assert [f.line for f in review.blocking] == [3]
    assert review.prompt_version == "reviewer-v1"
    assert review.truncated is False


async def test_invalid_json_retries_once_then_unavailable() -> None:
    llm = FakeLLM(["not json", "still not json"])
    with pytest.raises(ReviewUnavailable):
        await _review(llm)
    assert len(llm.requests) == 2


async def test_invalid_then_valid_json_recovers() -> None:
    llm = FakeLLM(["{bad", GOOD])
    review = await _review(llm)
    assert len(review.findings) == 2


async def test_schema_mismatch_is_invalid() -> None:
    bad = {"summary": "x", "findings": [{"path": "a", "line": 1, "severity": "meh", "body": "b"}]}
    with pytest.raises(ReviewUnavailable):
        await _review(FakeLLM([bad, bad]))


async def test_http_error_is_unavailable_without_key() -> None:
    with pytest.raises(ReviewUnavailable) as excinfo:
        await _review(FakeLLM([500, 500]))
    assert KEY not in str(excinfo.value)


async def test_large_diff_truncated_flag() -> None:
    hunk = "@@ -1 +1 @@\n" + "+" + "y" * 999 + "\n"
    diff = "diff --git a/x b/x\n" + hunk * 70  # ~70k chars
    llm = FakeLLM([GOOD])
    review = await _review(llm, diff)
    assert review.truncated is True
    sent = "".join(m["content"] for m in llm.requests[0]["messages"])
    assert len(sent) < 60000 + 8000  # budget + prompt/goal overhead
