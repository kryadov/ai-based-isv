"""llm MCP server: a single ``complete`` tool proxying the LiteLLM gateway.

The API key travels only in the ``Authorization: Bearer …`` header; it is never
logged, and upstream errors are redacted before they become tool errors.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 600.0
_REDACTED = "***"


def _completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return f"{base}/chat/completions"


def create_llm_server(
    *,
    base_url: str,
    api_key: str,
    default_model: str = "qwen3",
    http: httpx.AsyncClient | None = None,
) -> FastMCP:
    """Build the llm server over one LiteLLM gateway (``http`` is a test seam)."""
    client = http if http is not None else httpx.AsyncClient(timeout=LLM_TIMEOUT_SECONDS)
    url = _completions_url(base_url)
    mcp: FastMCP = FastMCP("llm")

    def _redact(text: str) -> str:
        return text.replace(api_key, _REDACTED) if api_key else text

    @mcp.tool
    async def complete(prompt: str, model: str = "", max_tokens: int = 256) -> str:
        """Complete ``prompt`` with a gateway model; returns the assistant text."""
        chosen = model or default_model
        body: dict[str, Any] = {
            "model": chosen,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        try:
            response = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=LLM_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            logger.info("op=complete model=%s outcome=transport_error", chosen)
            raise ToolError(_redact(f"gateway unreachable: {exc}")) from None
        if response.status_code >= 400:
            logger.info("op=complete model=%s outcome=http_%s", chosen, response.status_code)
            raise ToolError(_redact(f"gateway HTTP {response.status_code}: {response.text[:300]}"))
        try:
            text = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            logger.info("op=complete model=%s outcome=malformed", chosen)
            raise ToolError("gateway returned a malformed completion") from None
        logger.info("op=complete model=%s outcome=ok", chosen)
        return str(text or "")

    return mcp


def main() -> None:
    """Serve over streamable HTTP; config from env (LITELLM_BASE_URL, LITELLM_API_KEY, PORT)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = create_llm_server(
        base_url=os.environ["LITELLM_BASE_URL"],
        api_key=os.environ["LITELLM_API_KEY"],
        default_model=os.environ.get("LLM_DEFAULT_MODEL", "qwen3"),
    )
    app.run(
        transport="http",
        host="0.0.0.0",  # noqa: S104 - in-cluster service, fronted by NetworkPolicy
        port=int(os.environ.get("PORT", "8000")),
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
