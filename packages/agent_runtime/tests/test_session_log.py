"""Tests for the append-only NDJSON session log (Pattern 19: brain/hands/session)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from agent_runtime.session_log import SessionLog

_FILE = "session.ndjson"


def test_append_read_roundtrip(tmp_path: Path) -> None:
    log = SessionLog(tmp_path / _FILE)
    log.append("prompt", {"text": "Add reverse_words", "run_id": 1})
    log.append("harness_event", {"delta": "café ☕"})

    records = log.read_all()
    assert len(records) == 2
    assert [record["type"] for record in records] == ["prompt", "harness_event"]
    assert [record["seq"] for record in records] == [1, 2]
    assert records[0]["payload"] == {"text": "Add reverse_words", "run_id": 1}
    assert records[1]["payload"] == {"delta": "café ☕"}

    # ts — UTC ISO-8601, never going backwards inside one log
    stamps = [datetime.fromisoformat(str(record["ts"])) for record in records]
    assert all(stamp.utcoffset() == timedelta(0) for stamp in stamps)
    assert stamps[0] <= stamps[1]

    # the file is NDJSON: one JSON object per line, non-ASCII kept literal
    lines = (tmp_path / _FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1]) == records[1]
    assert "café ☕" in lines[1]


def test_seq_continues_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / _FILE
    first = SessionLog(path)
    first.append("run_started", {"attempt": 1})
    first.append("prompt", {"text": "go"})

    # a new process (crash-restart) reopens the same file and keeps numbering
    second = SessionLog(path)
    second.append("harness_event", {"delta": "still"})

    records = second.read_all()
    assert [record["seq"] for record in records] == [1, 2, 3]
    assert records[-1]["type"] == "harness_event"
    assert first.read_all() == records


def test_iter_type_filters(tmp_path: Path) -> None:
    log = SessionLog(tmp_path / _FILE)
    log.append("prompt", {"n": 1})
    log.append("harness_event", {"n": 2})
    log.append("harness_event", {"n": 3})
    log.append("done_when_check", {"n": 4})
    log.append("result", {"n": 5})

    events = list(log.iter_type("harness_event"))
    assert [event["payload"]["n"] for event in events] == [2, 3]
    assert [event["seq"] for event in events] == [2, 3]
    assert [record["seq"] for record in log.iter_type("result")] == [5]
    assert list(log.iter_type("missing_type")) == []


def test_read_all_on_missing_file_is_empty(tmp_path: Path) -> None:
    log = SessionLog(tmp_path / _FILE)
    assert log.read_all() == []
    assert list(log.iter_type("prompt")) == []

    log.append("run_started", {})
    assert (tmp_path / _FILE).exists()
