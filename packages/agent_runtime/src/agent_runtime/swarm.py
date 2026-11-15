"""Swarm mode CLI (M8, spec §7.2 item 3, §7.3): plan → parallel workers → judge.

    python -m agent_runtime.swarm plan  --run-id N --title T --body B --allowed a,b --done-cmd C
    python -m agent_runtime.swarm run   --workers 3 [--timeout S]   (worker env as usual)
    python -m agent_runtime.swarm judge --run-id N --done-cmd C [--project o/r]

- ``plan``: the coordinator (planner LLM) splits the task into a validated vertex
  graph; every vertex gets the platform-supplied ``done_when`` (never a
  planner-supplied command).
- ``run``: N worker processes on the same run — distinct lease identities
  (``<agent>-w<i>``) and workspaces (``<root>/w<i>``); the lease queue hands out
  independent vertices in parallel and dependents once their deps are done.
- ``judge``: all vertices done AND the merged main passes ``done_cmd`` → run
  ``done`` (+ a ``decision`` episode in project memory), else ``failed``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from agent_runtime.done_when import parse_done_when, validate_done_when
from agent_runtime.gitops import _git_env
from agent_runtime.memory import MemoryClient
from agent_runtime.planner import VertexPlan, plan_task

TERMINAL = frozenset({"done", "failed"})


async def post_plan(
    http: httpx.AsyncClient, *, run_id: int, plans: list[VertexPlan], done_cmd: str
) -> list[int]:
    done_when = json.dumps({"checks": [{"type": "cmd", "run": done_cmd}]})
    ids: list[int] = []
    for plan in plans:
        response = await http.post(
            f"/runs/{run_id}/vertices",
            json={
                "name": plan.name,
                "deps": plan.deps,
                "paths_allow": plan.paths_allow,
                "done_when": done_when,
            },
        )
        response.raise_for_status()
        ids.append(int(response.json()["vertex_id"]))
    return ids


def worker_env(base: dict[str, str], *, root: Path, index: int) -> dict[str, str]:
    env = dict(base)
    env["AGENT_RUNTIME_AGENT"] = f"{base.get('AGENT_RUNTIME_AGENT', 'swarm')}-w{index}"
    env["AGENT_RUNTIME_WORKSPACE_DIR"] = str(root / f"w{index}")
    return env


def all_terminal(vertices: list[dict[str, Any]]) -> bool:
    return bool(vertices) and all(v.get("status") in TERMINAL for v in vertices)


class Proc(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


@dataclass
class SuperviseReport:
    finished: bool
    crashed: list[int] = field(default_factory=list)


async def supervise(
    fetch: Callable[[], Awaitable[list[dict[str, Any]]]],
    procs: list[Any],
    *,
    timeout_s: float,
    poll_s: float = 15.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SuperviseReport:
    """Wait until the run graph is terminal (or timeout), then stop the workers.

    A crashed worker is reported, not fatal: its lease expires and another
    worker takes the vertex over (spec §7.3)."""
    report = SuperviseReport(finished=False)
    elapsed = 0.0
    while elapsed < timeout_s:
        for index, proc in enumerate(procs):
            if proc.poll() not in (None, 0) and index not in report.crashed:
                report.crashed.append(index)
        if all_terminal(await fetch()):
            report.finished = True
            break
        await sleep(poll_s)
        elapsed += poll_s
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=30)
    return report


@dataclass
class JudgeVerdict:
    status: Literal["done", "failed"]
    note: str


async def judge_run(
    http: httpx.AsyncClient,
    *,
    run_id: int,
    project: str,
    check_main: Callable[[], Awaitable[tuple[bool, str]]],
) -> JudgeVerdict:
    graph = (await http.get(f"/runs/{run_id}/vertices")).json().get("vertices", [])
    not_done = [str(v.get("name")) for v in graph if v.get("status") != "done"]
    if not graph or not_done:
        verdict = JudgeVerdict("failed", f"vertices not done: {', '.join(not_done) or '(none)'}")
    else:
        ok, detail = await check_main()
        verdict = (
            JudgeVerdict("done", f"{len(graph)} vertices done; main: {detail}")
            if ok
            else JudgeVerdict("failed", f"main check failed: {detail}")
        )
    await http.post(
        f"/runs/{run_id}/status",
        json={"status": verdict.status, "note": verdict.note, "actor": "judge"},
    )
    if verdict.status == "done":
        await MemoryClient(http, "").add(
            project,
            "decision",
            f"run {run_id} judged done: {len(graph)} vertices",
            verdict.note,
            sorted({p for v in graph for p in v.get("paths_allow", [])}),
            run_id=run_id,
        )
    return verdict


def _check_main_factory(
    repo_url: str, branch: str, done_cmd: str, token: str | None
) -> Callable[[], Awaitable[tuple[bool, str]]]:
    async def check() -> tuple[bool, str]:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "main"
            await asyncio.to_thread(
                subprocess.run,
                ["git", "clone", "-q", "--depth", "1", "--branch", branch, repo_url, str(dest)],
                env=_git_env(token),
                check=True,
                capture_output=True,
            )
            spec = parse_done_when(json.dumps({"checks": [{"type": "cmd", "run": done_cmd}]}))
            verdict = await asyncio.to_thread(validate_done_when, spec, cwd=dest)
            return verdict.passed, verdict.reason or "done_cmd passed"

    return check


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"AGENT_RUNTIME_{name}", default)


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Swarm mode (M8)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--run-id", type=int, required=True)
    p_plan.add_argument("--title", required=True)
    p_plan.add_argument("--body", default="")
    p_plan.add_argument("--allowed", required=True)
    p_plan.add_argument("--done-cmd", required=True)
    p_plan.add_argument("--model", default="qwen3")
    p_run = sub.add_parser("run")
    p_run.add_argument("--workers", type=int, default=3)
    p_run.add_argument("--timeout", type=float, default=5400.0)
    p_judge = sub.add_parser("judge")
    p_judge.add_argument("--run-id", type=int, required=True)
    p_judge.add_argument("--done-cmd", required=True)
    p_judge.add_argument("--project", default="")
    ns = parser.parse_args(argv)
    api = _env("API_URL", "http://127.0.0.1:8472")

    async with httpx.AsyncClient(base_url=api, timeout=60.0) as http:
        if ns.cmd == "plan":
            llm = (_env("REVIEWER_BASE_URL") or _env("LITELLM_BASE_URL")).rstrip("/")
            async with httpx.AsyncClient() as llm_http:
                plans = await plan_task(
                    llm_http,
                    base_url=llm if llm.endswith("/v1") else f"{llm}/v1",
                    api_key=_env("LITELLM_API_KEY"),
                    model=ns.model,
                    title=ns.title,
                    body=ns.body,
                    allowed=[a for a in ns.allowed.split(",") if a],
                )
            ids = await post_plan(http, run_id=ns.run_id, plans=plans, done_cmd=ns.done_cmd)
            for vid, plan in zip(ids, plans, strict=True):
                print(f"vertex {vid}: {plan.name} deps={plan.deps} paths={plan.paths_allow}")
            return 0
        if ns.cmd == "run":
            run_id = int(_env("RUN_ID", "0"))
            root = Path(_env("WORKSPACE_DIR", "./.agent-runtime-workspace"))
            base = {k: v for k, v in os.environ.items()}
            procs = []
            for i in range(1, ns.workers + 1):
                (root / f"w{i}").mkdir(parents=True, exist_ok=True)
                log = (root / f"w{i}.log").open("w", encoding="utf-8")  # noqa: SIM115
                procs.append(
                    subprocess.Popen(  # noqa: S603
                        [sys.executable, "-m", "agent_runtime.worker"],
                        env=worker_env(base, root=root, index=i),
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                )

            async def fetch() -> list[dict[str, Any]]:
                vertices: list[dict[str, Any]] = (
                    (await http.get(f"/runs/{run_id}/vertices")).json().get("vertices", [])
                )
                return vertices

            started = time.monotonic()
            report = await supervise(fetch, procs, timeout_s=ns.timeout)
            print(
                f"swarm run {run_id}: finished={report.finished} crashed={report.crashed} "
                f"in {time.monotonic() - started:.0f}s"
            )
            return 0 if report.finished else 1
        token = _env("GIT_TOKEN") or None
        verdict = await judge_run(
            http,
            run_id=ns.run_id,
            project=ns.project
            or _env("REPO_URL").rstrip("/").split("/", 3)[-1].removesuffix(".git"),
            check_main=_check_main_factory(
                _env("REPO_URL"), _env("BASE_BRANCH", "main"), ns.done_cmd, token
            ),
        )
        print(f"judge run {ns.run_id}: {verdict.status} — {verdict.note}")
        return 0 if verdict.status == "done" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
