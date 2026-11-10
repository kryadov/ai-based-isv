"""Tests for the append-only NDJSON session log (Pattern 19: brain/hands/session)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from agent_runtime.session_log import SessionLog

_FILE = "session.ndjson"

# A writer killed mid-append: a record fragment with no terminating newline.
_TORN = '{"ts": "2026-11-09T10:00:00+00:00", "seq": 2, "typ'


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


def test_read_skips_torn_final_line(tmp_path: Path) -> None:
    """Bytes after the last newline are an uncommitted partial write: skipped, not fatal."""
    path = tmp_path / _FILE
    log = SessionLog(path)
    log.append("prompt", {"n": 1})
    log.append("harness_event", {"n": 2})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_TORN)  # the writer died here

    reopened = SessionLog(path)
    records = reopened.read_all()
    assert [record["seq"] for record in records] == [1, 2]
    assert [record["type"] for record in records] == ["prompt", "harness_event"]
    assert len(list(reopened.iter_type("harness_event"))) == 1


def test_torn_tail_then_append_recovers(tmp_path: Path) -> None:
    """A crash fragment must not swallow the next record or poison later reads."""
    path = tmp_path / _FILE
    log = SessionLog(path)
    log.append("prompt", {"n": 1})
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_TORN)  # partial record, seq 5 claimed — never committed

    reopened = SessionLog(path)
    reopened.append("prompt", {"n": 2})
    reopened.append("result", {"n": 3})

    records = reopened.read_all()
    assert [record["seq"] for record in records] == [1, 2, 3]
    assert [record["payload"]["n"] for record in records] == [1, 2, 3]
    # nothing merged: every line is its own complete record, the fragment is dropped
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)


def test_malformed_committed_line_still_raises(tmp_path: Path) -> None:
    """A newline-terminated bad line is committed corruption: fail-closed, everywhere."""
    path = tmp_path / _FILE
    log = SessionLog(path)
    log.append("prompt", {"n": 1})
    log.append("harness_event", {"n": 2})
    first, second = path.read_text(encoding="utf-8").splitlines()
    path.write_text(f"{first}\ncorrupted****\n{second}\n", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        SessionLog(path).read_all()
    # iter_type reads through the same iterator — fail-closed there too
    with pytest.raises(json.JSONDecodeError):
        list(SessionLog(path).iter_type("prompt"))


def test_read_survives_tail_torn_inside_multibyte_char(tmp_path: Path) -> None:
    """A crash mid multi-byte character must not raise before the torn-tail guard:
    uncommitted bytes are never decoded, reads stop at the last committed newline."""
    path = tmp_path / _FILE
    log = SessionLog(path)
    log.append("prompt", {"text": "café ☕"})
    fragment = '{"ts": "2026-11-09T10:00:00+00:00", "seq": 2, "payload": {"x": "caf' + "é"
    with path.open("ab") as handle:
        handle.write(fragment.encode("utf-8")[:-1])  # ends on 0xc3 — inside the 'é'

    reopened = SessionLog(path)  # construction must not raise UnicodeDecodeError
    records = reopened.read_all()
    assert len(records) == 1
    assert records[0]["payload"] == {"text": "café ☕"}  # committed multi-byte decodes fine

    reopened.append("harness_event", {"n": 2})  # the recovery path stays reachable
    assert [record["seq"] for record in reopened.read_all()] == [1, 2]


def test_invalid_utf8_in_committed_line_still_raises(tmp_path: Path) -> None:
    """Strict decode is kept for committed records: invalid bytes are corruption,
    not a torn tail — they must raise, never be silently replaced."""
    path = tmp_path / _FILE
    path.write_bytes(b'{"seq": 1}\n\x80\x81 broken bytes\n{"seq": 2}\n')

    with pytest.raises(UnicodeDecodeError):
        SessionLog(path)
