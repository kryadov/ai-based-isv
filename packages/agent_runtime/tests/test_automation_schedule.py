"""Cron scheduling of automations (M11): the poller requests runs when a tick passed."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from agent_runtime.automations import cron_matches, due, latest_tick, schedule_tick


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


WEEKLY = {"name": "vuln-scan", "trigger": "cron", "schedule": "0 6 * * 1"}  # Mondays 06:00


@pytest.mark.parametrize(
    ("expr", "when", "ok"),
    [
        ("0 6 * * 1", "2026-11-16 06:00", True),  # a Monday
        ("0 6 * * 1", "2026-11-17 06:00", False),
        ("*/15 * * * *", "2026-11-16 10:45", True),
        ("*/15 * * * *", "2026-11-16 10:46", False),
        ("0 9-17/4 * * *", "2026-11-16 13:00", True),
        ("0 0 1,15 * *", "2026-11-15 00:00", True),
        ("0 0 * * 0", "2026-11-15 00:00", True),  # Sunday = 0
        ("0 0 * * 7", "2026-11-15 00:00", True),  # Sunday = 7
    ],
)
def test_cron_matches(expr: str, when: str, ok: bool) -> None:
    assert cron_matches(expr, _t(when)) is ok


@pytest.mark.parametrize("expr", ["", "* * * *", "61 * * * *", "a * * * *", "*/0 * * * *"])
def test_cron_invalid(expr: str) -> None:
    with pytest.raises(ValueError):
        cron_matches(expr, _t("2026-11-16 06:00"))


def test_latest_tick() -> None:
    assert latest_tick("0 6 * * 1", _t("2026-11-18 12:00")) == _t("2026-11-16 06:00")


def test_due_manual_never() -> None:
    manual = {"name": "x", "trigger": "manual"}
    assert not due(manual, None, _t("2026-11-16 07:00"), _t("2026-11-01 00:00"))


def test_due_after_tick() -> None:
    assert due(WEEKLY, _t("2026-11-09 06:00"), _t("2026-11-16 06:01"), _t("2026-11-01 00:00"))


def test_not_due_before_tick() -> None:
    assert not due(WEEKLY, _t("2026-11-09 06:00"), _t("2026-11-16 05:59"), _t("2026-11-01 00:00"))


def test_due_first_start_fires_once() -> None:
    started = _t("2026-11-16 05:00")
    # never ran: past ticks before the daemon started do not fire
    assert not due(WEEKLY, None, _t("2026-11-16 05:30"), started)
    # the first tick after start fires …
    assert due(WEEKLY, None, _t("2026-11-16 06:00"), started)
    # … and once its run exists, not again
    assert not due(WEEKLY, _t("2026-11-16 06:00"), _t("2026-11-16 06:30"), started)


def test_due_invalid_schedule_is_false() -> None:
    broken = {"name": "x", "trigger": "cron", "schedule": "nonsense"}
    assert not due(broken, None, _t("2026-11-16 06:00"), _t("2026-11-01 00:00"))


class FakeApi:
    def __init__(self, last: str | None) -> None:
        self.last = last
        self.posted: list[dict[str, Any]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        runs = [{"id": 1, "name": "vuln-scan", "created_at": self.last}] if self.last else []
        return _Resp({"runs": runs})

    async def post(self, path: str, json: dict[str, Any]) -> Any:
        self.posted.append(json)
        return _Resp({"id": 2})


class _Resp:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    def json(self) -> dict[str, Any]:
        return self.body

    def raise_for_status(self) -> None:
        return None


async def test_schedule_tick_posts_requested_run() -> None:
    api = FakeApi("2026-11-09 06:00:03.123+00:00")
    fired = await schedule_tick(
        api,  # type: ignore[arg-type]
        {"vuln-scan": WEEKLY, "m": {"name": "m", "trigger": "manual"}},
        now=_t("2026-11-16 06:00"),
        started_at=_t("2026-11-01 00:00"),
    )
    assert fired == ["vuln-scan"]
    assert api.posted == [{"name": "vuln-scan", "requested_by": "cron"}]


async def test_schedule_tick_reads_naive_sqlite_timestamps() -> None:
    api = FakeApi("2026-11-16 06:00:01")
    fired = await schedule_tick(
        api,  # type: ignore[arg-type]
        {"vuln-scan": WEEKLY},
        now=_t("2026-11-16 07:00"),
        started_at=_t("2026-11-01 00:00"),
    )
    assert fired == []
