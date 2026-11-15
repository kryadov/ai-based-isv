"""isv_product as a real service: business endpoint, Prometheus metrics, faults (M6)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from isv_product.main import create_app


def test_greet_ok() -> None:
    client = TestClient(create_app(error_rate=0.0, latency_ms=0))
    response = client.get("/api/greet", params={"name": "a"})
    assert response.status_code == 200
    assert response.json() == {"greeting": "hello, a"}


def test_metrics_expose_requests_and_latency() -> None:
    client = TestClient(create_app(error_rate=0.0, latency_ms=0))
    client.get("/api/greet", params={"name": "a"})
    text = client.get("/metrics").text
    assert 'http_requests_total{code="200",route="/api/greet"}' in text
    assert "http_request_duration_seconds_bucket" in text


def test_fault_error_rate_one_fails_api_only() -> None:
    client = TestClient(create_app(error_rate=1.0, latency_ms=0))
    assert client.get("/api/greet").status_code == 500
    assert client.get("/healthz").status_code == 200
    text = client.get("/metrics").text
    assert 'http_requests_total{code="500",route="/api/greet"}' in text


def test_fault_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULT_ERROR_RATE", "1")
    assert TestClient(create_app()).get("/api/greet").status_code == 500


def test_invalid_fault_env_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULT_ERROR_RATE", "abc")
    monkeypatch.setenv("FAULT_LATENCY_MS", "-5")
    assert TestClient(create_app()).get("/api/greet").status_code == 200


def test_apps_have_isolated_registries() -> None:
    a = TestClient(create_app(error_rate=0.0, latency_ms=0))
    b = TestClient(create_app(error_rate=0.0, latency_ms=0))
    a.get("/api/greet")
    assert 'route="/api/greet"' not in b.get("/metrics").text
