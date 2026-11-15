"""swarm CLI: plan → parallel workers → judge (M8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from agent_runtime.planner import VertexPlan
from agent_runtime.swarm import all_terminal, judge_run, post_plan, supervise, worker_env


class FakeAPI:
    def __init__(self, graph: list[dict[str, Any]] | None = None) -> None:
        self.graph = graph or []
        self.posted: list[dict[str, Any]] = []
        self.status: list[dict[str, Any]] = []
        self.episodes: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path.endswith("/vertices") and method == "POST":
            self.posted.append(body)
            return httpx.Response(200, json={"vertex_id": len(self.posted)})
        if path.endswith("/vertices") and method == "GET":
            return httpx.Response(200, json={"vertices": self.graph})
        if path.endswith("/status") and method == "POST":
            self.status.append(body)
            return httpx.Response(200, json={"status": body["status"]})
        if path == "/memory/episodes" and method == "POST":
            self.episodes.append(body)
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://api")


async def test_post_plan_uses_platform_done_when() -> None:
    api = FakeAPI()
    plans = [
        VertexPlan(name="slug", paths_allow=["src/slug.py"]),
        VertexPlan(name="doc", paths_allow=["docs/**"], deps=["slug"]),
    ]
    async with api.client() as http:
        ids = await post_plan(http, run_id=7, plans=plans, done_cmd="uv run pytest -q")
    assert ids == [1, 2]
    assert api.posted[1]["deps"] == ["slug"] and api.posted[1]["paths_allow"] == ["docs/**"]
    done_when = json.loads(api.posted[0]["done_when"])
    assert done_when == {"checks": [{"type": "cmd", "run": "uv run pytest -q"}]}


def test_worker_env_isolates_identity_and_workspace(tmp_path: Path) -> None:
    base = {"AGENT_RUNTIME_RUN_ID": "7", "AGENT_RUNTIME_AGENT": "swarm"}
    env = worker_env(base, root=tmp_path, index=2)
    assert env["AGENT_RUNTIME_AGENT"] == "swarm-w2"
    assert env["AGENT_RUNTIME_WORKSPACE_DIR"] == str(tmp_path / "w2")
    assert env["AGENT_RUNTIME_RUN_ID"] == "7" and base["AGENT_RUNTIME_AGENT"] == "swarm"


def test_all_terminal() -> None:
    assert all_terminal([{"status": "done"}, {"status": "failed"}])
    assert not all_terminal([{"status": "done"}, {"status": "running"}])
    assert not all_terminal([])


class FakeProc:
    def __init__(self, code: int | None = None) -> None:
        self.code = code
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return self.code or 0


async def test_supervise_stops_workers_when_graph_is_terminal() -> None:
    states = [[{"status": "running"}], [{"status": "done"}]]
    procs = [FakeProc(), FakeProc()]

    async def fetch() -> list[dict[str, Any]]:
        return states.pop(0) if len(states) > 1 else states[0]

    async def instant(_: float) -> None:
        return None

    report = await supervise(fetch, procs, timeout_s=10, poll_s=1, sleep=instant)
    assert report.finished and all(p.terminated for p in procs)
    assert report.crashed == []


async def test_supervise_reports_crashed_worker() -> None:
    procs = [FakeProc(code=1), FakeProc()]

    async def fetch() -> list[dict[str, Any]]:
        return [{"status": "done"}]

    async def instant(_: float) -> None:
        return None

    report = await supervise(fetch, procs, timeout_s=10, poll_s=1, sleep=instant)
    assert report.crashed == [0]


async def test_judge_passes_and_records_decision() -> None:
    api = FakeAPI([{"name": "a", "status": "done"}, {"name": "b", "status": "done"}])

    async def main_ok() -> tuple[bool, str]:
        return True, "pytest passed"

    async with api.client() as http:
        verdict = await judge_run(http, run_id=7, project="o/r", check_main=main_ok)
    assert verdict.status == "done"
    assert api.status == [{"status": "done", "note": "2 vertices done; main: pytest passed",
                           "actor": "judge"}]  # fmt: skip
    assert api.episodes and api.episodes[0]["kind"] == "decision"


async def test_judge_fails_on_failed_vertex_without_running_check() -> None:
    api = FakeAPI([{"name": "a", "status": "done"}, {"name": "b", "status": "failed"}])
    called: list[bool] = []

    async def main_ok() -> tuple[bool, str]:
        called.append(True)
        return True, ""

    async with api.client() as http:
        verdict = await judge_run(http, run_id=7, project="o/r", check_main=main_ok)
    assert verdict.status == "failed" and "b" in verdict.note
    assert called == []
    assert api.status[0]["status"] == "failed"


async def test_judge_fails_when_main_check_fails() -> None:
    api = FakeAPI([{"name": "a", "status": "done"}])

    async def main_bad() -> tuple[bool, str]:
        return False, "pytest exited 1"

    async with api.client() as http:
        verdict = await judge_run(http, run_id=7, project="o/r", check_main=main_bad)
    assert verdict.status == "failed" and "pytest exited 1" in verdict.note
