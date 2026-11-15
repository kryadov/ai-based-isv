"""Automations in the control plane (M10, addendum §2).

Definitions are code (``infra/automations/*.yaml``); the control plane lists
them and stores RUNS. Execution is host-side (``agent_runtime.automations``):
it polls ``requested`` runs, works, files a HITL card, and acts only after a
human approves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from agent_api.models import AutomationRunRow

RUN_STATUSES = frozenset({"requested", "running", "awaiting_approval", "done", "failed"})
_REQUIRED = ("name", "description", "trigger", "scope", "role", "workflow")


def load_definitions(directory: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Valid definitions (sorted by name) and the file names that failed to parse."""
    valid: list[dict[str, Any]] = []
    invalid: list[str] = []
    root = Path(directory)
    for path in sorted(root.glob("*.yaml")) if root.is_dir() else []:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            invalid.append(path.name)
            continue
        if not isinstance(data, dict) or any(k not in data for k in _REQUIRED):
            invalid.append(path.name)
            continue
        valid.append(data)
    return sorted(valid, key=lambda d: str(d["name"])), invalid


def _run_dict(row: AutomationRunRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "requested_by": row.requested_by,
        "report": row.report_json or {},
        "hitl_id": row.hitl_id,
        "created_issues": list(row.created_issues_json or []),
        "note": row.note,
        "created_at": str(row.created_at),
    }


def register_automations(app: FastAPI) -> None:
    def definition(name: str) -> dict[str, Any]:
        found = [d for d in load_definitions(app.state.automations_dir)[0] if d["name"] == name]
        if not found:
            raise HTTPException(status_code=404, detail=f"unknown automation {name!r}")
        return found[0]

    async def create_run(name: str, requested_by: str) -> int:
        definition(name)
        async with app.state.session_factory() as session:
            row = AutomationRunRow(name=name, requested_by=requested_by)
            session.add(row)
            await session.flush()
            await session.commit()
            return row.id

    app.state.create_automation_run = create_run

    @app.get("/automations")
    async def list_automations() -> dict[str, Any]:
        valid, invalid = load_definitions(app.state.automations_dir)
        return {"automations": valid, "invalid": invalid}

    @app.post("/automations/runs")
    async def post_run(payload: dict[str, Any]) -> dict[str, Any]:
        run_id = await create_run(
            str(payload.get("name", "")), str(payload.get("requested_by", ""))
        )
        return {"id": run_id}

    @app.patch("/automations/runs/{run_id}")
    async def patch_run(run_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if "status" in payload and payload["status"] not in RUN_STATUSES:
            raise HTTPException(
                status_code=422, detail=f"status must be one of {sorted(RUN_STATUSES)}"
            )
        async with app.state.session_factory() as session:
            row = (
                await session.scalars(select(AutomationRunRow).where(AutomationRunRow.id == run_id))
            ).first()
            if row is None:
                raise HTTPException(status_code=404, detail="run not found")
            if "status" in payload:
                row.status = str(payload["status"])
            if "report" in payload:
                row.report_json = dict(payload["report"] or {})
            if "hitl_id" in payload:
                row.hitl_id = int(payload["hitl_id"]) if payload["hitl_id"] is not None else None
            if "created_issues" in payload:
                row.created_issues_json = [str(u) for u in payload["created_issues"] or []]
            if "note" in payload:
                row.note = str(payload["note"])[:2000]
            await session.commit()
            return _run_dict(row)

    @app.get("/automations/runs")
    async def get_runs(name: str = "", status: str = "", limit: int = 50) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            stmt = select(AutomationRunRow).order_by(AutomationRunRow.id.desc()).limit(limit)
            if name:
                stmt = stmt.where(AutomationRunRow.name == name)
            if status:
                stmt = stmt.where(AutomationRunRow.status == status)
            return {"runs": [_run_dict(r) for r in await session.scalars(stmt)]}
