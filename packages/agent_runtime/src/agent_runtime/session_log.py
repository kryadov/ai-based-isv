"""Append-only NDJSON session log — Pattern 19 (brain/hands/session) evidence stream.

One record per line: ``{ts, seq, type, payload}``. The log is the durable trace of
an execution session and survives a worker crash: the file is only ever opened in
append mode, never rewritten, and a reopened log continues ``seq`` from the highest
number already on disk, so a replayed vertex never collides with the previous
attempt's records. A malformed line is a hard error — the log is evidence, silently
dropping or repairing records would hide corruption.

Tasks 7 and 9 write ``prompt``, ``harness_event``, ``done_when_check`` and ``result``
records here; the manifest (Task 5) points at this path as the run's evidence artifact.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class SessionLog:
    """An append-only NDJSON log bound to one session file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._seq = self._last_seq()

    def append(self, type_: str, payload: dict[str, Any]) -> None:
        """Append one ``{ts, seq, type, payload}`` record and return once it is on disk."""
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
        """Every record in the log, in append order; empty for a log not yet written."""
        return list(self._records())

    def iter_type(self, type_: str) -> Iterator[dict[str, Any]]:
        """Records of one type only, in append order."""
        return (record for record in self._records() if record["type"] == type_)

    def _records(self) -> Iterator[dict[str, Any]]:
        if not self._path.exists():
            return
        with self._path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    record: dict[str, Any] = json.loads(line)
                    yield record

    def _last_seq(self) -> int:
        return max((int(record["seq"]) for record in self._records()), default=0)
