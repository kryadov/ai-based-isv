"""Prometheus client for the deployer and the observability MCP (M6)."""

from __future__ import annotations

import json

import httpx
import pytest
from agent_runtime.prom import PromError, Slo, instant, service_slo


def _vector(*values: str) -> dict[str, object]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1, v]} for v in values],
        },
    }


def _client(responder: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(responder))  # type: ignore[arg-type]


async def test_instant_parses_first_sample() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["query"])
        return httpx.Response(200, json=_vector("0.25"))

    async with _client(respond) as http:
        assert await instant(http, "http://p:9090", "up") == 0.25
    assert seen == ["up"]


async def test_instant_empty_vector_is_none() -> None:
    async with _client(lambda r: httpx.Response(200, json=_vector())) as http:
        assert await instant(http, "http://p:9090", "up") is None


async def test_instant_nan_is_none() -> None:
    async with _client(lambda r: httpx.Response(200, json=_vector("NaN"))) as http:
        assert await instant(http, "http://p:9090", "up") is None


async def test_instant_http_error_raises() -> None:
    async with _client(lambda r: httpx.Response(500, text="boom")) as http:
        with pytest.raises(PromError):
            await instant(http, "http://p:9090", "up")


async def test_instant_error_status_raises() -> None:
    body = {"status": "error", "error": "parse error"}
    async with _client(lambda r: httpx.Response(200, content=json.dumps(body))) as http:
        with pytest.raises(PromError, match="parse error"):
            await instant(http, "http://p:9090", "bad(")


async def test_service_slo_empty_is_zero() -> None:
    async with _client(lambda r: httpx.Response(200, json=_vector())) as http:
        slo = await service_slo(http, "http://p:9090", namespace="prod", service="isv-product")
    assert slo == Slo(error_ratio=0.0, p95_s=0.0)


async def test_service_slo_queries_service_and_window() -> None:
    queries: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        q = request.url.params["query"]
        queries.append(q)
        return httpx.Response(200, json=_vector("0.02" if "5.." in q else "0.12"))

    async with _client(respond) as http:
        slo = await service_slo(
            http, "http://p:9090", namespace="prod", service="isv-product", window="5m"
        )
    assert slo == Slo(error_ratio=0.02, p95_s=0.12)
    assert all('service="isv-product"' in q and "[5m]" in q for q in queries)
