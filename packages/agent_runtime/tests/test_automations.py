"""Automation engine (M10): vuln-scan → report → HITL card → issues after approval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_runtime.automations import (
    Finding,
    act_on_decision,
    issue_body,
    parse_gitleaks,
    parse_semgrep,
    run_vuln_scan,
)

SEMGREP = {
    "results": [
        {
            "check_id": "python.lang.security.audit.subprocess-shell-true.subprocess-shell-true",
            "path": "app.py",
            "start": {"line": 5},
            "extra": {"severity": "ERROR", "message": "shell=True is dangerous"},
        }
    ],
    "errors": [],
}
GITLEAKS = [{"RuleID": "generic-api-key", "File": "cfg.env", "StartLine": 2, "Description": "key"}]
DEFINITION: dict[str, Any] = {
    "name": "vuln-scan",
    "scope": ["swarm-lab/vuln-demo", "swarm-lab/clean"],
    "semgrep_configs": ["p/ci", "p/python"],
}


def test_parsers() -> None:
    assert parse_semgrep(json.dumps(SEMGREP), repo="o/r") == [
        Finding(
            repo="o/r",
            tool="semgrep",
            rule="subprocess-shell-true",
            path="app.py",
            line=5,
            severity="ERROR",
            message="shell=True is dangerous",
        )
    ]
    assert parse_gitleaks(json.dumps(GITLEAKS), repo="o/r")[0].rule == "generic-api-key"
    assert parse_gitleaks("", repo="o/r") == []


def test_issue_body_lists_findings() -> None:
    body = issue_body("o/r", parse_semgrep(json.dumps(SEMGREP), repo="o/r"), run_id=3)
    assert "subprocess-shell-true" in body and "app.py:5" in body and "run 3" in body


class World:
    """Fake control plane + Gitea + tools."""

    def __init__(self, decision: str = "approved", crash: bool = False) -> None:
        self.decision = decision
        self.crash = crash
        self.patches: list[dict[str, Any]] = []
        self.hitl: list[dict[str, Any]] = []
        self.issues: list[tuple[str, dict[str, Any]]] = []

    def api(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.method == "PATCH":
            self.patches.append(body)
            return httpx.Response(200, json=body)
        if request.url.path == "/hitl" and request.method == "POST":
            self.hitl.append(body)
            return httpx.Response(200, json={"request_id": 9})
        if request.url.path == "/hitl/9":
            return httpx.Response(200, json={"id": 9, "status": self.decision})
        return httpx.Response(404)

    def gitea(self, request: httpx.Request) -> httpx.Response:
        repo = "/".join(request.url.path.split("/")[4:6])
        self.issues.append((repo, json.loads(request.content)))
        return httpx.Response(201, json={"html_url": f"http://g/{repo}/issues/1"})

    def tools(self, argv: list[str], cwd: Path) -> str:
        if self.crash:
            raise RuntimeError("semgrep exploded")
        dirty = "vuln-demo" in str(cwd)
        if "semgrep" in " ".join(argv):
            return json.dumps(SEMGREP if dirty else {"results": [], "errors": []})
        return json.dumps(GITLEAKS if dirty else [])


def _clients(world: World) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    return (
        httpx.AsyncClient(transport=httpx.MockTransport(world.api), base_url="http://api"),
        httpx.AsyncClient(transport=httpx.MockTransport(world.gitea), base_url="http://gitea"),
    )


async def _scan(world: World, tmp_path: Path) -> Any:
    api, _ = _clients(world)
    async with api:
        return await run_vuln_scan(
            api, run_id=3, definition=DEFINITION, workdir=tmp_path,
            clone=lambda repo, dest: dest.mkdir(parents=True), tools=world.tools,
        )  # fmt: skip


async def test_scan_files_card_with_findings(tmp_path: Path) -> None:
    world = World()
    report = await _scan(world, tmp_path)
    assert len(report["findings"]) == 2 and report["repos_with_findings"] == ["swarm-lab/vuln-demo"]
    card = world.hitl[0]
    assert card["kind"] == "automation" and "2 findings" in card["subject"]
    assert card["context"]["run_id"] == 3
    assert world.patches[0]["status"] == "running"
    assert world.patches[-1]["status"] == "awaiting_approval" and world.patches[-1]["hitl_id"] == 9


async def test_clean_scan_is_done_without_card(tmp_path: Path) -> None:
    world = World()
    clean = {**DEFINITION, "scope": ["swarm-lab/clean"]}
    api, _ = _clients(world)
    async with api:
        await run_vuln_scan(
            api, run_id=3, definition=clean, workdir=tmp_path,
            clone=lambda repo, dest: dest.mkdir(parents=True), tools=world.tools,
        )  # fmt: skip
    assert world.hitl == [] and world.patches[-1]["status"] == "done"


async def test_tool_crash_fails_run_without_card(tmp_path: Path) -> None:
    world = World(crash=True)
    with pytest.raises(RuntimeError):
        await _scan(world, tmp_path)
    assert world.hitl == []
    assert (
        world.patches[-1]["status"] == "failed" and "semgrep exploded" in world.patches[-1]["note"]
    )


@pytest.mark.parametrize(("decision", "issues"), [("approved", 1), ("rejected", 0)])
async def test_act_on_decision(tmp_path: Path, decision: str, issues: int) -> None:
    world = World(decision=decision)
    report = await _scan(world, tmp_path)
    api, gitea = _clients(world)
    async with api, gitea:
        result = await act_on_decision(api, gitea, run_id=3, hitl_id=9, report=report)
    assert result == decision
    assert len(world.issues) == issues
    if issues:
        repo, issue = world.issues[0]
        assert repo == "swarm-lab/vuln-demo" and issue["title"].startswith("[vuln-scan]")
        assert world.patches[-1] == {
            "status": "done",
            "created_issues": ["http://g/swarm-lab/vuln-demo/issues/1"],
        }
    else:
        assert world.patches[-1]["status"] == "done" and "rejected" in world.patches[-1]["note"]


async def test_pending_decision_does_nothing(tmp_path: Path) -> None:
    world = World(decision="pending")
    report = await _scan(world, tmp_path)
    api, gitea = _clients(world)
    async with api, gitea:
        assert await act_on_decision(api, gitea, run_id=3, hitl_id=9, report=report) == "pending"
    assert world.issues == []
