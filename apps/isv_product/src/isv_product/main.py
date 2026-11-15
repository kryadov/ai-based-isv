"""isv_product — the organization's demo service (spec §6, M6 canary target).

A small real service with honest SLO semantics: a business endpoint, Prometheus
metrics per route and status code, and opt-in fault injection so a canary
rollout can be shown failing its analysis without shipping broken code
(``FAULT_ERROR_RATE`` 0..1 → HTTP 500 on ``/api/*``; ``FAULT_LATENCY_MS`` →
added delay). Faults default to off; invalid values count as off.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram
from prometheus_client.exposition import generate_latest

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0)


def _env_float(name: str, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, "0"))
    except ValueError:
        return 0.0
    return value if low <= value <= high else 0.0


def create_app(
    *,
    error_rate: float | None = None,
    latency_ms: int | None = None,
    rng: Callable[[], float] = random.random,
) -> FastAPI:
    """Build the app; ``None`` fault settings are read from the environment."""
    rate = _env_float("FAULT_ERROR_RATE", 0.0, 1.0) if error_rate is None else error_rate
    delay_s = (
        _env_float("FAULT_LATENCY_MS", 0.0, 60_000.0) if latency_ms is None else latency_ms
    ) / 1000.0

    app = FastAPI(title="isv_product")
    registry = CollectorRegistry()
    requests_total = Counter(
        "http_requests_total", "HTTP requests", ["route", "code"], registry=registry
    )
    duration = Histogram(
        "http_request_duration_seconds",
        "HTTP request latency",
        ["route"],
        buckets=_LATENCY_BUCKETS,
        registry=registry,
    )

    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path == "/metrics":
            return await call_next(request)
        started = time.perf_counter()
        if path.startswith("/api/"):
            if delay_s:
                await asyncio.sleep(delay_s)
            if rate and rng() < rate:
                response: Response = JSONResponse({"error": "injected fault"}, status_code=500)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        route = request.scope.get("route")
        template = getattr(route, "path", path)
        requests_total.labels(route=template, code=str(response.status_code)).inc()
        duration.labels(route=template).observe(time.perf_counter() - started)
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/greet")
    def greet(name: str = "world") -> dict[str, str]:
        return {"greeting": f"hello, {name}"}

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
