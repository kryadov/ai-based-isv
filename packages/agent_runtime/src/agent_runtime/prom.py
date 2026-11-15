"""Minimal Prometheus HTTP API client (M6: deployer baseline, observability MCP).

Only instant queries. An empty vector or a NaN sample means "no data" (``None``);
HTTP or query errors raise :class:`PromError` — a caller deciding a rollout must
never mistake an unreachable Prometheus for a healthy service.
"""

from __future__ import annotations

import math

import httpx
from pydantic import BaseModel


class PromError(RuntimeError):
    """Prometheus was unreachable or rejected the query."""


class Slo(BaseModel):
    """Service-wide request health: 5xx ratio and p95 latency (seconds)."""

    error_ratio: float
    p95_s: float


async def instant(http: httpx.AsyncClient, base_url: str, promql: str) -> float | None:
    """Value of the first sample of an instant query, or ``None`` when there is none."""
    try:
        response = await http.get(f"{base_url.rstrip('/')}/api/v1/query", params={"query": promql})
    except httpx.HTTPError as exc:
        raise PromError(f"prometheus unreachable: {exc}") from exc
    if response.status_code != 200:
        raise PromError(f"prometheus HTTP {response.status_code}: {response.text[:200]}")
    body = response.json()
    if body.get("status") != "success":
        raise PromError(f"prometheus query failed: {body.get('error', body)}")
    result = body.get("data", {}).get("result", [])
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if math.isnan(value) else value


def _selector(namespace: str, service: str) -> str:
    return f'namespace="{namespace}",service="{service}",route=~"/api/.*"'


async def service_slo(
    http: httpx.AsyncClient,
    base_url: str,
    *,
    namespace: str,
    service: str,
    window: str = "5m",
) -> Slo:
    """Current 5xx ratio and p95 of ``service``'s API routes; no data → 0.0."""
    sel = _selector(namespace, service)
    errors = await instant(
        http,
        base_url,
        f'sum(rate(http_requests_total{{{sel},code=~"5.."}}[{window}]))'
        f" / sum(rate(http_requests_total{{{sel}}}[{window}]))",
    )
    p95 = await instant(
        http,
        base_url,
        "histogram_quantile(0.95, sum by (le) "
        f"(rate(http_request_duration_seconds_bucket{{{sel}}}[{window}])))",
    )
    return Slo(error_ratio=errors or 0.0, p95_s=p95 or 0.0)
