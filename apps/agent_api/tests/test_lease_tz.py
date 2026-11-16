"""Lease expiry with Postgres timestamptz (M11, found live): asyncpg returns
aware datetimes while the store's clock is naive UTC; comparing them raised
TypeError and every claim in a run with a leased vertex answered 500."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_api.store import lease_expired


def test_aware_lease_in_the_past_is_expired() -> None:
    now = datetime(2026, 11, 16, 14, 0)  # naive UTC (the store's clock)
    assert lease_expired(datetime(2026, 11, 16, 13, 55, tzinfo=UTC), now)


def test_aware_lease_in_the_future_is_live() -> None:
    now = datetime(2026, 11, 16, 14, 0)
    assert not lease_expired(datetime(2026, 11, 16, 14, 4, tzinfo=UTC), now)


def test_naive_lease_and_none() -> None:
    now = datetime(2026, 11, 16, 14, 0)
    assert lease_expired(now - timedelta(seconds=1), now)
    assert not lease_expired(None, now)
