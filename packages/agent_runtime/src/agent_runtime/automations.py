"""Automation engine (M10, addendum §2): host-side runner of org automations.

    python -m agent_runtime.automations run vuln-scan     # manual / cron
    python -m agent_runtime.automations poll [--once]     # runs requested from the panel

A run works, stores a REPORT, and — when there is something to act on — files
an Inbox card (HITL kind ``automation``). Actions (creating fix tasks in the
tracker) happen ONLY after a human approves the card; a rejection does nothing.

Implemented workflow (v0): ``vuln-scan`` — semgrep (configs from the
definition) + gitleaks over every repo in scope → findings → one tracker issue
per affected repo after approval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from agent_runtime.gitops import _git_env

GITLEAKS_IMAGE = "zricethezav/gitleaks:v8.28.0"
TOP_FINDINGS = 20

Tools = Callable[[list[str], Path], str]
Clone = Callable[[str, Path], None]


@dataclass(frozen=True)
class Finding:
    repo: str
    tool: str
    rule: str
    path: str
    line: int
    severity: str
    message: str


def parse_semgrep(text: str, *, repo: str) -> list[Finding]:
    data = json.loads(text or "{}")
    return [
        Finding(
            repo=repo,
            tool="semgrep",
            rule=str(r.get("check_id", "")).rsplit(".", 1)[-1],
            path=str(r.get("path", "")),
            line=int((r.get("start") or {}).get("line", 0)),
            severity=str((r.get("extra") or {}).get("severity", "INFO")),
            message=str((r.get("extra") or {}).get("message", ""))[:300],
        )
        for r in data.get("results", [])
    ]


def parse_gitleaks(text: str, *, repo: str) -> list[Finding]:
    data = json.loads(text) if text.strip() else []
    return [
        Finding(
            repo=repo,
            tool="gitleaks",
            rule=str(r.get("RuleID", "")),
            path=str(r.get("File", "")),
            line=int(r.get("StartLine", 0) or 0),
            severity="ERROR",
            message=str(r.get("Description", ""))[:300],
        )
        for r in data
    ]


def issue_body(repo: str, findings: list[Finding], *, run_id: int) -> str:
    lines = [
        f"Automated **vuln-scan** findings for `{repo}` (automation run {run_id}),",
        "approved by a human in the Inbox. Fix each item or justify it in a PR.",
        "",
        "| tool | rule | location | severity |",
        "|---|---|---|---|",
    ]
    for f in findings:
        lines.append(f"| {f.tool} | `{f.rule}` | `{f.path}:{f.line}` | {f.severity} |")
    return "\n".join(lines) + "\n"


async def _patch(api: httpx.AsyncClient, run_id: int, **fields: Any) -> None:
    await api.patch(f"/automations/runs/{run_id}", json=fields)


def _scan_repo(repo: str, dest: Path, definition: dict[str, Any], tools: Tools) -> list[Finding]:
    findings: list[Finding] = []
    for config in definition.get("semgrep_configs", ["p/ci"]):
        out = tools(
            ["uvx", "--with", "pydantic<2.12", "semgrep==1.140.0", "scan", "--config", str(config),
             "--json", "--metrics", "off", "--quiet", "."],
            dest,
        )  # fmt: skip
        findings += parse_semgrep(out, repo=repo)
    out = tools(
        ["docker", "run", "--rm", "-v", f"{dest}:/repo", GITLEAKS_IMAGE, "detect",
         "--source", "/repo", "--no-git", "--redact", "--exit-code", "0",
         "--report-format", "json", "--report-path", "/dev/stdout", "--log-level", "error"],
        dest,
    )  # fmt: skip
    findings += parse_gitleaks(out, repo=repo)
    unique: dict[tuple[str, str, str, int], Finding] = {}
    for f in findings:
        unique.setdefault((f.tool, f.rule, f.path, f.line), f)
    return list(unique.values())


async def run_vuln_scan(
    api: httpx.AsyncClient,
    *,
    run_id: int,
    definition: dict[str, Any],
    workdir: Path,
    clone: Clone,
    tools: Tools,
) -> dict[str, Any]:
    """Scan every repo in scope; file the Inbox card when something was found."""
    await _patch(api, run_id, status="running")
    try:
        findings: list[Finding] = []
        for repo in definition.get("scope", []):
            dest = workdir / repo.replace("/", "__")
            await asyncio.to_thread(clone, repo, dest)
            findings += await asyncio.to_thread(_scan_repo, repo, dest, definition, tools)
    except Exception as exc:
        await _patch(api, run_id, status="failed", note=f"{type(exc).__name__}: {exc}")
        raise
    affected = sorted({f.repo for f in findings})
    report = {
        "findings": [asdict(f) for f in findings],
        "repos": list(definition.get("scope", [])),
        "repos_with_findings": affected,
    }
    if not findings:
        await _patch(api, run_id, status="done", report=report, note="no findings")
        return report
    summary = f"vuln-scan: {len(findings)} findings in {len(affected)} repo(s)"
    response = await api.post(
        "/hitl",
        json={
            "kind": "automation",
            "subject": summary,
            "context": {
                "run_id": run_id,
                "automation": definition.get("name"),
                "summary": f"{summary}. Approve to create one fix task per repo.",
                "findings": report["findings"][:TOP_FINDINGS],
            },
        },
    )
    response.raise_for_status()
    hitl_id = int(response.json()["request_id"])
    await _patch(api, run_id, status="awaiting_approval", report=report, hitl_id=hitl_id)
    return report


async def act_on_decision(
    api: httpx.AsyncClient,
    gitea: httpx.AsyncClient,
    *,
    run_id: int,
    hitl_id: int,
    report: dict[str, Any],
) -> str:
    """After the human decision: approved → one issue per affected repo; else nothing."""
    status = str((await api.get(f"/hitl/{hitl_id}")).json().get("status", "pending"))
    if status == "pending":
        return status
    if status != "approved":
        await _patch(api, run_id, status="done", note=f"card {status}: no action taken")
        return status
    created: list[str] = []
    for repo in report.get("repos_with_findings", []):
        items = [Finding(**f) for f in report["findings"] if f["repo"] == repo]
        response = await gitea.post(
            f"/api/v1/repos/{repo}/issues",
            json={
                "title": f"[vuln-scan] {len(items)} finding(s) to fix",
                "body": issue_body(repo, items, run_id=run_id),
            },
        )
        response.raise_for_status()
        created.append(str(response.json().get("html_url", "")))
    await _patch(api, run_id, status="done", created_issues=created)
    return status


# --------------------------------------------------------------------------- host wiring


def _run_tool(argv: list[str], cwd: Path) -> str:
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603
    if done.returncode != 0 and not done.stdout.strip():
        raise RuntimeError(f"{argv[0]} failed ({done.returncode}): {done.stderr.strip()[-300:]}")
    return done.stdout


def _cloner(gitea_url: str, token: str) -> Clone:
    def clone(repo: str, dest: Path) -> None:
        subprocess.run(  # noqa: S603, S607
            [
                "git",
                "clone",
                "-q",
                "--depth",
                "1",
                f"{gitea_url.rstrip('/')}/{repo}.git",
                str(dest),
            ],
            env=_git_env(token),
            check=True,
            capture_output=True,
        )

    return clone


async def _execute(api: httpx.AsyncClient, run: dict[str, Any], definitions: dict[str, Any],
                   gitea_url: str, token: str, workroot: Path) -> None:  # fmt: skip
    definition = definitions.get(run["name"])
    if definition is None or definition.get("workflow") != "vuln-scan":
        await _patch(
            api, run["id"], status="failed", note="no runnable workflow for this automation"
        )
        return
    workroot.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=workroot) as tmp:
        await run_vuln_scan(
            api, run_id=run["id"], definition=definition, workdir=Path(tmp),
            clone=_cloner(gitea_url, token), tools=_run_tool,
        )  # fmt: skip


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Automation engine (M10)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("name")
    p_poll = sub.add_parser("poll")
    p_poll.add_argument("--once", action="store_true")
    p_poll.add_argument("--interval", type=float, default=30.0)
    ns = parser.parse_args(argv)
    api_url = os.environ.get("AGENT_RUNTIME_API_URL", "http://127.0.0.1:8472")
    gitea_url = os.environ.get("AUTOMATIONS_GITEA_URL", "http://localhost:30080")
    token = os.environ["AUTOMATIONS_GITEA_TOKEN"]
    workroot = Path(os.environ.get("AUTOMATIONS_WORKDIR", "./.agent-runtime-workspace/automations"))
    async with (
        httpx.AsyncClient(base_url=api_url, timeout=60.0) as api,
        httpx.AsyncClient(
            base_url=gitea_url, headers={"Authorization": f"token {token}"}, timeout=60.0
        ) as gitea,
    ):
        definitions = {d["name"]: d for d in (await api.get("/automations")).json()["automations"]}
        if ns.cmd == "run":
            created = await api.post(
                "/automations/runs", json={"name": ns.name, "requested_by": "cli"}
            )
            run_id = int(created.json()["id"])
            await _execute(
                api, {"id": run_id, "name": ns.name}, definitions, gitea_url, token, workroot
            )
            print(f"automation {ns.name}: run {run_id} executed")
            return 0
        while True:
            for run in (await api.get("/automations/runs", params={"status": "requested"})).json()[
                "runs"
            ]:
                try:
                    await _execute(api, run, definitions, gitea_url, token, workroot)
                    print(f"automation run {run['id']} ({run['name']}) executed")
                except Exception as exc:
                    print(f"automation run {run['id']} failed: {exc}")
            awaiting = (
                await api.get("/automations/runs", params={"status": "awaiting_approval"})
            ).json()
            for run in awaiting["runs"]:
                decision = await act_on_decision(
                    api, gitea, run_id=run["id"], hitl_id=run["hitl_id"], report=run["report"]
                )
                if decision != "pending":
                    print(f"automation run {run['id']}: card {decision}")
            if ns.once:
                return 0
            await asyncio.sleep(ns.interval)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
