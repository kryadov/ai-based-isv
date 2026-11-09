"""Append-only NDJSON session log — Pattern 19 (brain/hands/session) evidence stream.

One record per line: ``{ts, seq, type, payload}``. The log is the durable trace of
an execution session and survives a worker crash. Committed records are only ever
appended, never rewritten; a record is committed when its terminating newline is
written, so bytes after the last newline belong to an unfinished write.

Crash policy (pinned by tests):

* a torn tail (a final line without a trailing newline) is uncommitted — ``read_all``
  and ``iter_type`` stop before it without raising even when the crash cut a record
  mid multi-byte character (uncommitted bytes are never decoded), a reopened log
  numbers its next record from the last committed ``seq``, and ``append`` truncates
  those bytes before writing, so a crash fragment can neither merge with (and
  destroy) the next record nor linger as corruption in the committed prefix;
* a malformed *committed* line (newline-terminated, or mid-file) is a hard error —
  the log is evidence, silently dropping or repairing corruption in committed
  records would hide it.

**Single-writer invariant**: exactly one ``SessionLog`` instance may write to a given
path at a time. ``seq`` is a per-instance counter seeded from the file tail at
construction, so two concurrent instances on one file would duplicate seq numbers.
The executor session owns the log's only instance: Task 7 pumps ``prompt`` and
``harness_event`` records through it, and Task 9's ``done_when_check`` and ``result``
records go through that same instance or only after it is done writing. The manifest
(Task 5) points at this path as the run's evidence artifact.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class SessionLog:
    """An append-only NDJSON log bound to one session file (single writer, see above)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._seq = self._last_seq()

    def append(self, type_: str, payload: dict[str, Any]) -> None:
        """Append one ``{ts, seq, type, payload}`` record and return once it is on disk."""
        self._discard_uncommitted_tail()
        self._seq += 1
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "seq": self._seq,
            "type": type_,
            "payload": payload,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")

    def read_all(self) -> list[dict[str, Any]]:
        """Committed records in append order; empty for a log not yet written."""
        return list(self._records())

    def iter_type(self, type_: str) -> Iterator[dict[str, Any]]:
        """Committed records of one type only, in append order."""
        return (record for record in self._records() if record["type"] == type_)

    def _records(self) -> Iterator[dict[str, Any]]:
        if not self._path.exists():
            return
        # Binary framing: the uncommitted tail is never decoded, so a crash that cut
        # a record mid multi-byte character cannot raise before the torn-tail guard;
        # committed lines are decoded strictly — invalid bytes there are corruption.
        with self._path.open("rb") as handle:
            for line in handle:
                if not line.endswith(b"\n"):
                    # Torn tail: bytes of a write that crashed before committing.
                    return
                if line.strip():
                    record: dict[str, Any] = json.loads(line)
                    yield record

    def _discard_uncommitted_tail(self) -> None:
        """Truncate bytes after the last committed newline (crash recovery).

        Only uncommitted fragments are dropped — committed records are never touched.
        The steady-state check reads one byte at end-of-file; the full scan happens
        only on the first append after a crash.
        """
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if size == 0:
            return
        with self._path.open("rb") as handle:
            handle.seek(-1, io.SEEK_END)
            if handle.read(1) == b"\n":
                return
        with self._path.open("rb+") as handle:
            data = handle.read()
            handle.seek(data.rfind(b"\n") + 1)
            handle.truncate()

    def _last_seq(self) -> int:
        return max((int(record["seq"]) for record in self._records()), default=0)
