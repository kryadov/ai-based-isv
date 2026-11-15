"""observability MCP server: read-only rollout state and service metrics (M6).

Backed by Prometheus only (Argo Rollouts controller metrics + service metrics):
agents never reach the Kubernetes API.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp_servers.observability_server import MAX_SERIES, create_observability_app

PROM = "http://prom.test:9090"


def _vector(series: list[tuple[dict[str, str], str]]) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": m, "value": [1, v]} for m, v in series],
        },
    }


class FakeProm:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params["query"]
        self.queries.append(q)
        if q.startswith("rollout_info"):
            return httpx.Response(
                200, json=_vector([({"name": "isv-product", "phase": "Progressing"}, "1")])
            )
        if q.startswith("analysis_run_info"):
            return httpx.Response(
                200,
                json=_vector(
                    [
                        ({"name": "isv-product-abc-2", "phase": "Successful"}, "1"),
                        ({"name": "isv-product-abc-2-5", "phase": "Running"}, "1"),
                    ]
                ),
            )
        if "5.." in q:
            return httpx.Response(200, json=_vector([({}, "0.02")]))
        if "histogram_quantile" in q:
            return httpx.Response(200, json=_vector([({}, "0.11")]))
        if q == "many":
            return httpx.Response(200, json=_vector([({"i": str(i)}, "1") for i in range(80)]))
        return httpx.Response(200, json=_vector([({"job": "x"}, "1")]))


def _app(fake: FakeProm) -> Any:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return create_observability_app(prometheus_url=PROM, http=http)


async def test_get_rollout_from_controller_metrics() -> None:
    fake = FakeProm()
    async with Client(_app(fake)) as client:
        result = await client.call_tool("get_rollout", {"app": "isv-product"})
    data = result.structured_content
    assert data["phase"] == "Progressing"
    assert {r["name"]: r["phase"] for r in data["analysis_runs"]} == {
        "isv-product-abc-2": "Successful",
        "isv-product-abc-2-5": "Running",
    }
    assert any('rollout="isv-product"' in q or 'name="isv-product"' in q for q in fake.queries)


async def test_service_slo() -> None:
    async with Client(_app(FakeProm())) as client:
        result = await client.call_tool("service_slo", {"app": "isv-product"})
    assert result.structured_content == {"error_ratio": 0.02, "p95_s": 0.11}


async def test_query_metric_caps_series() -> None:
    async with Client(_app(FakeProm())) as client:
        result = await client.call_tool("query_metric", {"promql": "many"})
    data = result.structured_content
    assert len(data["series"]) == MAX_SERIES == 50
    assert data["truncated"] is True


@pytest.mark.parametrize("promql", ["", " ", "x" * 2001])
async def test_query_metric_rejects_bad_input(promql: str) -> None:
    async with Client(_app(FakeProm())) as client:
        with pytest.raises(ToolError):
            await client.call_tool("query_metric", {"promql": promql})


async def test_bad_app_name_rejected() -> None:
    async with Client(_app(FakeProm())) as client:
        with pytest.raises(ToolError):
            await client.call_tool("get_rollout", {"app": 'x"} or vector(1)'})


async def test_no_write_tools() -> None:
    async with Client(_app(FakeProm())) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"get_rollout", "service_slo", "query_metric"}
