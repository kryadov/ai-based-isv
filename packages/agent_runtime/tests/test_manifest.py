"""Tests for the run manifest — machine-readable record of one vertex execution."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agent_runtime.executor import ExecutionRequest
from agent_runtime.manifest import build_manifest, write_manifest

_REQUIRED_KEYS = {
    "run_id",
    "vertex_id",
    "name",
    "image",
    "model",
    "llm_endpoint",
    "ts_start",
    "ts_end",
    "session_log_path",
    "diff_stat",
    "tokens_used",
}

_TS_START = datetime(2026, 11, 9, 10, 0, 0, tzinfo=UTC)
_TS_END = datetime(2026, 11, 9, 10, 5, 30, tzinfo=UTC)


def _request() -> ExecutionRequest:
    return ExecutionRequest(
        run_id=1,
        vertex_id=2,
        name="add-reverse-words",
        prompt="Add reverse_words to src/wordtools/reverse.py and cover it with a test",
        paths_allow=["src/wordtools/**", "tests/**"],
        done_when='{"checks": [{"type": "cmd", "run": "pytest -q"}]}',
        repo_url="http://gitea-http.swarm-lab.svc:3000/swarm-lab/target-scratch.git",
        work_branch="agent/vertex-2",
    )


def _manifest(ts_end: datetime | None = _TS_END) -> dict[str, object]:
    return build_manifest(
        request=_request(),
        image="ghcr.io/anomalyco/opencode:1.14.0",
        model="qwen3",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        ts_start=_TS_START,
        ts_end=ts_end,
        session_log_path=Path("/evidence/run-1/vertex-2/session.ndjson"),
        diff_stat="2 files changed, 10 insertions(+), 1 deletion(-)",
        tokens_used=1234,
    )


def test_manifest_has_required_keys() -> None:
    manifest = _manifest()

    assert set(manifest) == _REQUIRED_KEYS
    # coordinates come from the request
    assert manifest["run_id"] == 1
    assert manifest["vertex_id"] == 2
    assert manifest["name"] == "add-reverse-words"
    # runtime pinning — what exactly was executed
    assert manifest["image"] == "ghcr.io/anomalyco/opencode:1.14.0"
    assert manifest["model"] == "qwen3"
    assert manifest["llm_endpoint"] == "http://litellm.swarm-lab.svc:30084/v1"
    assert manifest["diff_stat"] == "2 files changed, 10 insertions(+), 1 deletion(-)"
    assert manifest["tokens_used"] == 1234
    # timestamps serialize as ISO-8601 strings, paths as str: the dict is JSON-ready
    assert manifest["ts_start"] == "2026-11-09T10:00:00+00:00"
    assert manifest["ts_end"] == "2026-11-09T10:05:30+00:00"
    assert datetime.fromisoformat(str(manifest["ts_start"])) == _TS_START
    assert manifest["session_log_path"] == "/evidence/run-1/vertex-2/session.ndjson"
    json.dumps(manifest)  # no TypeError: everything is JSON-serializable


def test_write_and_reread(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    manifest = _manifest()

    write_manifest(path, manifest)

    # the JSON on disk equals the dict, byte-for-object
    assert json.loads(path.read_text(encoding="utf-8")) == manifest
    # human-readable: indent=2 (a non-trivial evidence artifact, not a one-liner)
    text = path.read_text(encoding="utf-8")
    assert text != json.dumps(manifest)
    assert '\n  "run_id": 1,' in text


def test_running_manifest_finalized_in_place(tmp_path: Path) -> None:
    """Session start writes the manifest with ts_end=None; completion rewrites the file."""
    path = tmp_path / "manifest.json"

    started = build_manifest(
        request=_request(),
        image="ghcr.io/anomalyco/opencode:1.14.0",
        model="qwen3",
        llm_endpoint="http://litellm.swarm-lab.svc:30084/v1",
        ts_start=_TS_START,
        ts_end=None,
        session_log_path=Path("/evidence/run-1/vertex-2/session.ndjson"),
    )
    assert started["ts_end"] is None
    assert started["diff_stat"] is None and started["tokens_used"] is None
    write_manifest(path, started)

    # the adapter finalizes in place (overwrite allowed) and may merge extra
    # evidence keys — e.g. the worker's verdict — before the final write
    finalized = _manifest()
    finalized["verdict"] = {"passed": True}
    write_manifest(path, finalized)

    reread = json.loads(path.read_text(encoding="utf-8"))
    assert reread == finalized
    assert reread["ts_end"] == "2026-11-09T10:05:30+00:00"
    assert reread["verdict"] == {"passed": True}
