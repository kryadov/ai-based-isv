"""Run manifest: the machine-readable record of one vertex execution.

Written at session start with ``ts_end=None`` and finalized in place on completion
by the OpenCode adapter (Task 7); it becomes the core of the evidence pack in M4b.
The dict is JSON-ready: timestamps are ISO-8601 strings, the session log path a
plain string — callers pass aware (UTC) datetimes, as ``SessionLog`` does.

``build_manifest`` produces the v0 schema below and is pure; extra evidence keys
(e.g. the worker's ``verdict``) are merged into the dict by the caller before the
final ``write_manifest``, so the manifest keeps growing without widening this API.

Required keys: ``run_id``, ``vertex_id``, ``name``, ``image``, ``model``,
``llm_endpoint``, ``ts_start``, ``ts_end``, ``session_log_path``, ``diff_stat``,
``tokens_used``. Design: spec 2026-11-09-m4-agent-runtime-design.md.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_runtime.executor import ExecutionRequest


def build_manifest(
    *,
    request: ExecutionRequest,
    image: str,
    model: str,
    llm_endpoint: str,
    ts_start: datetime,
    ts_end: datetime | None,
    session_log_path: Path,
    diff_stat: str | None = None,
    tokens_used: int | None = None,
) -> dict[str, Any]:
    """Assemble the manifest for one run of one vertex (no side effects)."""
    return {
        "run_id": request.run_id,
        "vertex_id": request.vertex_id,
        "name": request.name,
        "image": image,
        "model": model,
        "llm_endpoint": llm_endpoint,
        "ts_start": ts_start.isoformat(),
        "ts_end": ts_end.isoformat() if ts_end is not None else None,
        "session_log_path": str(session_log_path),
        "diff_stat": diff_stat,
        "tokens_used": tokens_used,
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Serialize the manifest as pretty JSON (indent=2), overwriting an existing file.

    Overwrite is the point: the adapter finalizes the manifest of a running session
    in place. The caller owns the parent directory (the executor creates the
    evidence directory before writing).
    """
    path.write_text(f"{json.dumps(manifest, indent=2, ensure_ascii=False)}\n", encoding="utf-8")
