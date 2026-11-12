"""llm MCP server proxying LiteLLM over httpx.MockTransport (M4c Task 3)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp_servers.llm_server import create_llm_server

KEY = "sk-litellm-master-SECRET"


def _completion(text: str) -> dict[str, object]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 12},
    }


async def test_complete_posts_chat_completion_with_bearer_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion("pong"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_llm_server(
        base_url="http://litellm.test:4000/v1", api_key=KEY, default_model="qwen3", http=http
    )
    async with Client(app) as client:
        result = await client.call_tool("complete", {"prompt": "ping", "max_tokens": 16})
    assert result.data == "pong"
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == "http://litellm.test:4000/v1/chat/completions"
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(req.content)
    assert body["model"] == "qwen3"
    assert body["max_tokens"] == 16
    assert body["messages"] == [{"role": "user", "content": "ping"}]


async def test_base_url_without_v1_is_normalised_and_model_override_wins() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion("ok"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_llm_server(base_url="http://litellm.test:4000", api_key=KEY, http=http)
    async with Client(app) as client:
        await client.call_tool("complete", {"prompt": "p", "model": "qwen-coder"})
    assert str(seen[0].url) == "http://litellm.test:4000/v1/chat/completions"
    assert json.loads(seen[0].content)["model"] == "qwen-coder"


async def test_upstream_error_is_tool_error_with_key_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="mcp_servers.llm_server")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid key {KEY}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_llm_server(base_url="http://litellm.test:4000/v1", api_key=KEY, http=http)
    async with Client(app) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("complete", {"prompt": "p"})
    assert "401" in str(excinfo.value)
    assert KEY not in str(excinfo.value)
    assert KEY not in caplog.text


async def test_malformed_completion_is_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_llm_server(base_url="http://litellm.test:4000/v1", api_key=KEY, http=http)
    async with Client(app) as client:
        with pytest.raises(ToolError):
            await client.call_tool("complete", {"prompt": "p"})
