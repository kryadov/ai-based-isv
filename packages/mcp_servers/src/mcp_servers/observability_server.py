"""observability MCP server (M6): read-only rollout state and service metrics.

Backed by Prometheus ONLY — the Argo Rollouts controller exports ``rollout_info``
and ``analysis_run_info``, the services export request metrics — so agents get
deploy visibility without any Kubernetes API access. No write tools.
"""

from __future__ import annotations

import logging
import math
import os
import re
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

MAX_SERIES = 50
MAX_QUERY_CHARS = 2000
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def _name(value: str, what: str) -> str:
    if not _NAME_RE.match(value):
        raise ToolError(f"invalid {what} {value!r}")
    return value


class _Prom:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    async def vector(self, promql: str) -> list[dict[str, Any]]:
        try:
            response = await self._http.get(f"{self._base}/api/v1/query", params={"query": promql})
        except httpx.HTTPError as exc:
            raise ToolError(f"prometheus unreachable: {exc}") from None
        if response.status_code != 200:
            raise ToolError(f"prometheus HTTP {response.status_code}")
        body = response.json()
        if body.get("status") != "success":
            raise ToolError(f"query failed: {body.get('error', 'unknown')}")
        result: list[dict[str, Any]] = body.get("data", {}).get("result", [])
        return result

    async def scalar(self, promql: str) -> float:
        series = await self.vector(promql)
        if not series:
            return 0.0
        value = float(series[0]["value"][1])
        return 0.0 if math.isnan(value) else value


def create_observability_app(
    *, prometheus_url: str, http: httpx.AsyncClient | None = None
) -> FastMCP:
    prom = _Prom(http or httpx.AsyncClient(timeout=30.0), prometheus_url)
    mcp = FastMCP("observability")

    @mcp.tool
    async def get_rollout(app: str = "isv-product", namespace: str = "prod") -> dict[str, Any]:
        """Canary rollout phase and its analysis runs (Argo Rollouts controller metrics)."""
        name = _name(app, "app")
        ns = _name(namespace, "namespace")
        logger.info("get_rollout %s/%s", ns, name)
        # The controller lives in `platform`; Prometheus keeps the Rollout's own
        # namespace as `exported_namespace` (live lesson, M6).
        info = await prom.vector(f'rollout_info{{exported_namespace="{ns}",name="{name}"}}')
        runs = await prom.vector(
            f'analysis_run_info{{exported_namespace="{ns}",name=~"{name}-.*"}}'
        )
        return {
            "app": name,
            "phase": info[0]["metric"].get("phase", "unknown") if info else "unknown",
            "analysis_runs": [
                {"name": r["metric"].get("name", ""), "phase": r["metric"].get("phase", "")}
                for r in runs[:MAX_SERIES]
            ],
        }

    @mcp.tool
    async def service_slo(
        app: str = "isv-product", namespace: str = "prod", window: str = "5m"
    ) -> dict[str, float]:
        """Current 5xx ratio and p95 latency (seconds) of the service's API routes."""
        name = _name(app, "app")
        ns = _name(namespace, "namespace")
        if not re.match(r"^[1-9][0-9]?[smh]$", window):
            raise ToolError(f"invalid window {window!r}")
        sel = f'namespace="{ns}",service="{name}",route=~"/api/.*"'
        errors = await prom.scalar(
            f'sum(rate(http_requests_total{{{sel},code=~"5.."}}[{window}]))'
            f" / sum(rate(http_requests_total{{{sel}}}[{window}]))"
        )
        p95 = await prom.scalar(
            "histogram_quantile(0.95, sum by (le) "
            f"(rate(http_request_duration_seconds_bucket{{{sel}}}[{window}])))"
        )
        return {"error_ratio": errors, "p95_s": p95}

    @mcp.tool
    async def query_metric(promql: str) -> dict[str, Any]:
        """Instant PromQL query (read-only); at most 50 series are returned."""
        text = promql.strip()
        if not text or len(promql) > MAX_QUERY_CHARS:
            raise ToolError(f"promql must be 1..{MAX_QUERY_CHARS} characters")
        logger.info("query_metric %s", text[:200])
        series = await prom.vector(text)
        return {
            "series": [
                {"labels": s["metric"], "value": s["value"][1]} for s in series[:MAX_SERIES]
            ],
            "truncated": len(series) > MAX_SERIES,
        }

    return mcp


def main() -> None:
    """Serve over streamable HTTP. Config from env: PROMETHEUS_URL, PORT."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = create_observability_app(prometheus_url=os.environ["PROMETHEUS_URL"])
    # FastMCP (not Flask) bound in a pod; the NetworkPolicy is the boundary.
    # nosemgrep: python.flask.security.audit.app-run-param-config.avoid_app_run_with_bad_host
    app.run(
        transport="http",
        host="0.0.0.0",  # noqa: S104 - in-cluster service, fronted by NetworkPolicy
        port=int(os.environ.get("PORT", "8000")),
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
