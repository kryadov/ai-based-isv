"""Standing dispatcher (M11): tracker issues become swarm runs, with no flow script.

Every tick:

1. reads ``.platform/project.yaml`` of every org repo; only ``intake: true``
   repos take part (an invalid file skips the repo with the reason, never the
   tick);
2. walks the runs it owns (``project`` = ``intake:<owner/repo>``) that are
   still ``pending``:
   - a run without vertices (planner failed, daemon restarted mid-plan) is
     planned again — never a second run;
   - a run whose vertices are all terminal, or whose issue is gone with no
     worker left, is judged and the verdict is commented on the issue;
   - a run with open vertices and no live workers (daemon restarted) gets its
     workers back — expired leases are reclaimed by the lease queue;
3. turns open ``swarm``-labelled issues without any run (of any status, by
   anyone) into new runs while fewer than ``max_active_runs`` are active and
   the sandbox budget (the NodePort pool) has room.

The dispatcher owns the decisions; every side effect goes through ``Ports``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_runtime.project_config import ProjectConfig, parse_project_config
from agent_runtime.swarm import TERMINAL, JudgeVerdict, Proc

INTAKE_PREFIX = "intake:"
RETRY_LABEL = "swarm-retry"  # M12: a human asks for another run of a failed task


@dataclass
class Task:
    repo: str
    number: int
    title: str
    body: str
    open: bool = True
    labels: tuple[str, ...] = field(default=())

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"


class Ports(Protocol):
    async def list_repos(self) -> list[str]: ...
    async def read_config(self, repo: str) -> str | None: ...
    async def list_tasks(self, repo: str) -> list[Task]: ...
    async def comment(self, repo: str, number: int, text: str) -> None: ...
    async def remove_label(self, repo: str, number: int, label: str) -> None: ...
    async def plan(self, run_id: int, task: Task, cfg: ProjectConfig) -> int: ...
    def spawn(self, run_id: int, task_ref: str, cfg: ProjectConfig, workers: int) -> list[Any]: ...
    async def judge(self, run_id: int, repo: str, cfg: ProjectConfig) -> JudgeVerdict: ...
    async def api_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]: ...
    async def api_post(self, path: str, json: dict[str, Any]) -> dict[str, Any]: ...


def _issue_number(task_ref: str) -> int:
    return int(task_ref.rsplit("#", 1)[1])


class Dispatcher:
    def __init__(self, ports: Ports, *, max_active_runs: int = 2, sandbox_cap: int = 11) -> None:
        self.ports = ports
        self.max_active_runs = max_active_runs
        self.sandbox_cap = sandbox_cap
        self.procs: dict[int, list[Proc]] = {}

    # --- workers -----------------------------------------------------------------
    def _live(self, run_id: int) -> int:
        return sum(1 for p in self.procs.get(run_id, []) if p.poll() is None)

    def _reap(self) -> None:
        self.procs = {
            run_id: [p for p in procs if p.poll() is None]
            for run_id, procs in self.procs.items()
            if any(p.poll() is None for p in procs)
        }

    def _sandboxes_free(self) -> int:
        return self.sandbox_cap - sum(self._live(r) for r in self.procs)

    def _spawn(self, run_id: int, task_ref: str, cfg: ProjectConfig, open_vertices: int) -> int:
        workers = min(open_vertices, cfg.workers_per_run, self._sandboxes_free())
        if workers <= 0:
            return 0
        self.procs.setdefault(run_id, []).extend(self.ports.spawn(run_id, task_ref, cfg, workers))
        return workers

    # --- steps -------------------------------------------------------------------
    async def _intake_repos(self, events: list[str]) -> dict[str, ProjectConfig]:
        repos: dict[str, ProjectConfig] = {}
        for repo in await self.ports.list_repos():
            text = await self.ports.read_config(repo)
            if text is None:
                continue
            try:
                cfg = parse_project_config(text)
            except ValueError as exc:
                events.append(f"{repo}: skipped ({exc})")
                continue
            if cfg.intake:
                repos[repo] = cfg
        return repos

    async def _plan(self, run_id: int, task: Task, cfg: ProjectConfig, events: list[str]) -> int:
        try:
            count = await self.ports.plan(run_id, task, cfg)
        except Exception as exc:  # the run stays vertex-less; the next tick re-plans it
            events.append(f"run {run_id} ({task.ref}): planning failed: {exc}")
            return 0
        workers = self._spawn(run_id, task.ref, cfg, count)
        await self.ports.comment(
            task.repo,
            task.number,
            f"Platform: run {run_id} planned {count} vertex(es); {workers} agent(s) started. "
            "Merge decisions appear in the panel Inbox.",
        )
        events.append(f"run {run_id} ({task.ref}): planned {count}, workers {workers}")
        return count

    async def _judge(self, run: dict[str, Any], repo: str, cfg: ProjectConfig) -> str:
        run_id = int(run["id"])
        verdict = await self.ports.judge(run_id, repo, cfg)
        await self.ports.comment(
            repo,
            _issue_number(str(run["task_ref"])),
            f"Platform: run {run_id} judged **{verdict.status}** — {verdict.note}",
        )
        return f"run {run_id} ({run['task_ref']}): judged {verdict.status}"

    async def _advance(
        self,
        run: dict[str, Any],
        repos: dict[str, ProjectConfig],
        open_tasks: dict[str, Task],
        events: list[str],
    ) -> bool:
        """Move one owned pending run forward; True while it stays active."""
        run_id = int(run["id"])
        repo = str(run["project"])[len(INTAKE_PREFIX) :]
        cfg = repos.get(repo)
        task = open_tasks.get(str(run["task_ref"]))
        if cfg is None:
            if self._live(run_id):
                return True
            await self.ports.api_post(
                f"/runs/{run_id}/status",
                {"status": "failed", "note": "repo intake disabled", "actor": "dispatcher"},
            )
            events.append(f"run {run_id}: failed (repo intake disabled)")
            return False
        if int(run.get("vertex_count", 0)) == 0:
            if task is None:
                await self.ports.api_post(
                    f"/runs/{run_id}/status",
                    {
                        "status": "failed",
                        "note": "issue closed before planning",
                        "actor": "dispatcher",
                    },
                )
                events.append(f"run {run_id}: failed (issue closed before planning)")
                return False
            await self._plan(run_id, task, cfg, events)
            return True
        vertices = (await self.ports.api_get(f"/runs/{run_id}/vertices", {})).get("vertices", [])
        open_vertices = sum(1 for v in vertices if v.get("status") not in TERMINAL)
        live = self._live(run_id)
        if open_vertices == 0 or (task is None and not live):
            events.append(await self._judge(run, repo, cfg))
            return False
        if not live and task is not None:
            workers = self._spawn(run_id, str(run["task_ref"]), cfg, open_vertices)
            if workers:
                events.append(f"run {run_id}: {workers} worker(s) (re)started")
        return True

    async def _retry_allowed(
        self, task: Task, newest: dict[str, Any], workers: int, events: list[str]
    ) -> bool:
        """The ``swarm-retry`` label (M12): only a failed newest run gets a new one.

        The label is consumed either way, with a comment saying what happened; the
        new run itself is created (and planned) by the caller.
        """
        status = str(newest.get("status", ""))
        run_id = int(newest["id"])
        if status == "failed":
            if self._sandboxes_free() < workers:
                return False  # keep the label: retried once sandboxes free up
            note = f"Platform: retry requested — run {run_id} failed; starting a new run."
            events.append(f"{task.ref}: retry after failed run {run_id}")
            allowed = True
        elif status == "done":
            note = f"Platform: retry ignored — run {run_id} is already done."
            allowed = False
        else:
            note = f"Platform: retry ignored — run {run_id} is still running ({status})."
            allowed = False
        await self.ports.remove_label(task.repo, task.number, RETRY_LABEL)
        await self.ports.comment(task.repo, task.number, note)
        return allowed

    async def tick(self) -> list[str]:
        events: list[str] = []
        self._reap()
        repos = await self._intake_repos(events)
        tasks = {repo: await self.ports.list_tasks(repo) for repo in repos}
        open_tasks = {t.ref: t for ts in tasks.values() for t in ts if t.open}

        pending = (await self.ports.api_get("/runs", {"status": "pending", "limit": 200}))["runs"]
        active = 0
        for run in pending:
            if str(run.get("project", "")).startswith(INTAKE_PREFIX):
                active += await self._advance(run, repos, open_tasks, events)

        for repo, repo_tasks in tasks.items():
            cfg = repos[repo]
            for task in repo_tasks:
                if not task.open or active >= self.max_active_runs:
                    continue
                existing = await self.ports.api_get("/runs", {"task_ref": task.ref, "limit": 1})
                newest = existing["runs"][0] if existing["runs"] else None
                retry = newest is not None and RETRY_LABEL in task.labels
                if newest is not None and not (
                    retry and await self._retry_allowed(task, newest, cfg.workers_per_run, events)
                ):
                    continue
                if self._sandboxes_free() < cfg.workers_per_run:
                    events.append(f"{task.ref}: waiting for sandboxes")
                    continue
                created = await self.ports.api_post(
                    "/runs", {"project": f"{INTAKE_PREFIX}{repo}", "task_ref": task.ref}
                )
                active += 1
                await self._plan(int(created["run_id"]), task, cfg, events)
        return events
