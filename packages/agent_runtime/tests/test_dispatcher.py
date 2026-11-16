"""Standing dispatcher (M11): tracker issue → run → plan → workers → judge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.dispatcher import INTAKE_PREFIX, Dispatcher, Task
from agent_runtime.project_config import ProjectConfig
from agent_runtime.swarm import JudgeVerdict

CFG = 'intake: true\npaths: ["src/**"]\ndone_when: "pytest"\nworkers_per_run: 2\n'


class FakeProc:
    def __init__(self) -> None:
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.code = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.code or 0


@dataclass
class FakePorts:
    configs: dict[str, str | None] = field(default_factory=dict)
    tasks: dict[str, list[Task]] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)
    vertices: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    comments: list[tuple[str, int, str]] = field(default_factory=list)
    spawned: list[tuple[int, int]] = field(default_factory=list)
    procs: list[FakeProc] = field(default_factory=list)
    judged: list[int] = field(default_factory=list)
    plan_size: int = 2
    plan_error: bool = False
    fail_list_repos: bool = False

    async def list_repos(self) -> list[str]:
        if self.fail_list_repos:
            raise RuntimeError("gitea down")
        return list(self.configs)

    async def read_config(self, repo: str) -> str | None:
        return self.configs[repo]

    async def list_tasks(self, repo: str) -> list[Task]:
        return [t for t in self.tasks.get(repo, []) if t.open]

    async def comment(self, repo: str, number: int, text: str) -> None:
        self.comments.append((repo, number, text))

    async def plan(self, run_id: int, task: Task, cfg: ProjectConfig) -> int:
        if self.plan_error:
            raise RuntimeError("planner LLM unavailable")
        self.vertices[run_id] = [
            {"id": i, "name": f"v{i}", "status": "pending"} for i in range(self.plan_size)
        ]
        self._run(run_id)["vertex_count"] = self.plan_size
        return self.plan_size

    def spawn(self, run_id: int, repo: str, cfg: ProjectConfig, workers: int) -> list[FakeProc]:
        self.spawned.append((run_id, workers))
        new = [FakeProc() for _ in range(workers)]
        self.procs.extend(new)
        return new

    async def judge(self, run_id: int, repo: str, cfg: ProjectConfig) -> JudgeVerdict:
        self.judged.append(run_id)
        done = all(v["status"] == "done" for v in self.vertices.get(run_id, [])) and bool(
            self.vertices.get(run_id)
        )
        self._run(run_id)["status"] = "done" if done else "failed"
        return JudgeVerdict("done" if done else "failed", "verdict note")

    async def api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if path == "/runs":
            out = [
                r
                for r in reversed(self.runs)
                if (not params.get("status") or r["status"] == params["status"])
                and (not params.get("task_ref") or r["task_ref"] == params["task_ref"])
            ]
            return {"runs": out}
        run_id = int(path.split("/")[2])
        return {"vertices": self.vertices.get(run_id, [])}

    async def api_post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        if path == "/runs":
            run = {
                "id": len(self.runs) + 1,
                "project": json["project"],
                "task_ref": json["task_ref"],
                "status": "pending",
                "vertex_count": 0,
            }
            self.runs.append(run)
            return {"run_id": run["id"]}
        run_id = int(path.split("/")[2])
        self._run(run_id)["status"] = json["status"]
        return {}

    def _run(self, run_id: int) -> dict[str, Any]:
        return next(r for r in self.runs if r["id"] == run_id)


def _ports(**kw: Any) -> FakePorts:
    ports = FakePorts(configs={"swarm-lab/demo": CFG}, **kw)
    ports.tasks.setdefault("swarm-lab/demo", [Task("swarm-lab/demo", 7, "Add /ping", "body", True)])
    return ports


async def test_tick_creates_run_plans_and_spawns() -> None:
    ports = _ports()
    await Dispatcher(ports).tick()
    assert [(r["project"], r["task_ref"]) for r in ports.runs] == [
        (f"{INTAKE_PREFIX}swarm-lab/demo", "swarm-lab/demo#7")
    ]
    assert ports.spawned == [(1, 2)]
    assert ports.comments and ports.comments[0][:2] == ("swarm-lab/demo", 7)
    assert "run 1" in ports.comments[0][2]


async def test_tick_skips_task_with_existing_run() -> None:
    ports = _ports()
    dispatcher = Dispatcher(ports)
    await dispatcher.tick()
    await dispatcher.tick()
    assert len(ports.runs) == 1
    assert ports.spawned == [(1, 2)]  # live workers are not doubled


async def test_tick_skips_task_whose_run_already_finished() -> None:
    ports = _ports()
    ports.runs.append(
        {
            "id": 1,
            "project": "flow",
            "task_ref": "swarm-lab/demo#7",
            "status": "done",
            "vertex_count": 1,
        }
    )
    await Dispatcher(ports).tick()
    assert len(ports.runs) == 1


async def test_tick_respects_max_active_runs() -> None:
    ports = _ports()
    ports.tasks["swarm-lab/demo"] = [
        Task("swarm-lab/demo", n, f"t{n}", "", True) for n in (1, 2, 3)
    ]
    await Dispatcher(ports, max_active_runs=2).tick()
    assert [r["task_ref"] for r in ports.runs] == ["swarm-lab/demo#1", "swarm-lab/demo#2"]


async def test_tick_respects_sandbox_cap() -> None:
    ports = _ports()
    ports.tasks["swarm-lab/demo"] = [Task("swarm-lab/demo", n, f"t{n}", "", True) for n in (1, 2)]
    await Dispatcher(ports, max_active_runs=5, sandbox_cap=3).tick()
    assert [r["task_ref"] for r in ports.runs] == ["swarm-lab/demo#1"]


async def test_tick_replans_run_without_vertices() -> None:
    ports = _ports(plan_error=True)
    dispatcher = Dispatcher(ports)
    events = await dispatcher.tick()
    assert len(ports.runs) == 1 and ports.runs[0]["vertex_count"] == 0
    assert any("planner LLM unavailable" in e for e in events)
    ports.plan_error = False
    await Dispatcher(ports).tick()  # e.g. after a daemon restart
    assert len(ports.runs) == 1
    assert ports.runs[0]["vertex_count"] == 2
    assert ports.spawned == [(1, 2)]


async def test_tick_judges_terminal_run_and_comments() -> None:
    ports = _ports()
    dispatcher = Dispatcher(ports)
    await dispatcher.tick()
    for vertex in ports.vertices[1]:
        vertex["status"] = "done"
    for proc in ports.procs:
        proc.code = 0
    await dispatcher.tick()
    assert ports.judged == [1]
    assert ports.runs[0]["status"] == "done"
    assert "done" in ports.comments[-1][2] and "verdict note" in ports.comments[-1][2]
    await dispatcher.tick()
    assert ports.judged == [1]  # judged once


async def test_tick_skips_repo_with_invalid_config() -> None:
    ports = _ports()
    ports.configs["swarm-lab/broken"] = "intake: true\npaths: []\ndone_when: x\n"
    ports.configs["swarm-lab/none"] = None
    ports.tasks["swarm-lab/broken"] = [Task("swarm-lab/broken", 1, "t", "", True)]
    events = await Dispatcher(ports).tick()
    assert [r["task_ref"] for r in ports.runs] == ["swarm-lab/demo#7"]
    assert any("swarm-lab/broken" in e and "skipped" in e for e in events)


async def test_tick_skips_repo_with_intake_false() -> None:
    ports = _ports()
    ports.configs["swarm-lab/demo"] = CFG.replace("intake: true", "intake: false")
    await Dispatcher(ports).tick()
    assert ports.runs == []


async def test_tick_respawns_workers_for_adopted_runs() -> None:
    ports = _ports()
    await Dispatcher(ports).tick()
    # the daemon restarts: a fresh dispatcher has no live procs for run 1
    await Dispatcher(ports).tick()
    assert ports.spawned == [(1, 2), (1, 2)]
    assert len(ports.runs) == 1


async def test_tick_does_not_respawn_for_closed_issue() -> None:
    ports = _ports()
    await Dispatcher(ports).tick()
    ports.tasks["swarm-lab/demo"][0].open = False
    await Dispatcher(ports).tick()  # restarted: no live workers, issue closed
    assert ports.spawned == [(1, 2)]
    assert ports.judged == [1]
    assert ports.runs[0]["status"] == "failed"


async def test_tick_ignores_runs_it_does_not_own() -> None:
    ports = _ports()
    ports.tasks["swarm-lab/demo"] = []
    ports.runs.append(
        {
            "id": 1,
            "project": "swarm",
            "task_ref": "swarm-lab/demo#3",
            "status": "pending",
            "vertex_count": 0,
        }
    )
    await Dispatcher(ports).tick()
    assert ports.spawned == [] and ports.judged == []
