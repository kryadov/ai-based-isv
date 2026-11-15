"""Organization panel routes (M10): ``/ui`` shell + ``/ui/api`` for humans.

Reads reuse the control-plane store; every ACTION goes through ``/ui/api`` behind
a signed session and is recorded under the human's name. Without configured
users the panel is disabled (503) — it never runs open.
"""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from agent_api import store
from agent_api.models import EpisodeRow, HITLRow, RunRow, VertexRow
from agent_api.ui_auth import COOKIE, SESSION_TTL_S, UI_DIR, check_password, sign, verify


def _page(name: str, status: int = 200, **subs: str) -> HTMLResponse:
    html = (UI_DIR / name).read_text(encoding="utf-8")
    for key, value in subs.items():
        html = html.replace(f"{{{{{key}}}}}", value)
    return HTMLResponse(html, status_code=status)


def register_ui(app: FastAPI) -> None:
    def current_user(request: Request) -> str | None:
        token = request.cookies.get(COOKIE, "")
        user = verify(token, app.state.session_secret) if token else None
        return user if user and user in app.state.ui_users else None

    def require_user(request: Request) -> str:
        if not app.state.ui_users:
            raise HTTPException(status_code=503, detail="panel disabled: set AGENT_API_UI_USERS")
        user = current_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="login required")
        return user

    @app.get("/ui", response_class=HTMLResponse)
    async def ui_root(request: Request) -> HTMLResponse:
        if not app.state.ui_users or not app.state.session_secret:
            return HTMLResponse(
                "<h1>Panel disabled</h1><p>Set AGENT_API_UI_USERS and "
                "AGENT_API_SESSION_SECRET to enable human login.</p>",
                status_code=503,
            )
        if current_user(request) is None:
            return _page("login.html", error="")
        return _page("index.html")

    @app.post("/ui/login", response_model=None)
    async def ui_login(request: Request) -> Response:
        form = parse_qs((await request.body()).decode("utf-8"))
        name = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]
        user = app.state.ui_users.get(name)
        if user is None or not app.state.session_secret or not check_password(user, password):
            return _page("login.html", status=401, error="Invalid username or password")
        response = RedirectResponse("/ui", status_code=303)
        response.set_cookie(
            COOKIE,
            sign(name, app.state.session_secret),
            max_age=SESSION_TTL_S,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.post("/ui/logout")
    async def ui_logout() -> RedirectResponse:
        response = RedirectResponse("/ui", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/ui/static/{path:path}")
    async def ui_static(path: str) -> FileResponse:
        target = (UI_DIR / path).resolve()
        if not target.is_relative_to(UI_DIR) or not target.is_file():
            raise HTTPException(status_code=404, detail="not found")
        media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(target, media_type=media)

    @app.get("/ui/api/me")
    async def ui_me(request: Request) -> dict[str, str]:
        user = require_user(request)
        return {"user": user, "role": app.state.ui_users[user].role}

    @app.post("/ui/api/hitl/{request_id}/decision")
    async def ui_decide(request_id: int, request: Request) -> dict[str, Any]:
        user = require_user(request)
        payload = await request.json()
        action = str(payload.get("action", ""))
        if action not in ("approve", "reject"):
            raise HTTPException(status_code=422, detail="action must be approve or reject")
        async with app.state.session_factory() as session:
            status = await store.decide_hitl(
                session, request_id, action=action, decided_by=user, note=payload.get("note")
            )
            if status is None:
                row = (
                    await session.scalars(select(HITLRow).where(HITLRow.id == request_id))
                ).first()
                raise HTTPException(
                    status_code=409 if row else 404,
                    detail="already decided" if row else "not found",
                )
            await store.append_audit(
                session,
                actor=f"ui:{user}",
                action=f"hitl_{action}",
                target=f"hitl:{request_id}",
                payload={"note": payload.get("note")},
            )
        return {"id": request_id, "status": status, "decided_by": user}

    @app.get("/ui/api/inbox")
    async def ui_inbox(request: Request) -> dict[str, Any]:
        require_user(request)
        async with app.state.session_factory() as session:
            rows = await session.scalars(
                select(HITLRow).where(HITLRow.status == "pending").order_by(HITLRow.id)
            )
            now = datetime.now(UTC)
            cards = []
            for r in rows:
                created = getattr(r, "created_at", None)
                if created is not None and created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                cards.append(
                    {
                        "id": r.id,
                        "kind": r.kind,
                        "subject": r.subject,
                        "context": r.context_json or {},
                        "waiting_s": int((now - created).total_seconds()) if created else None,
                    }
                )
            return {"cards": cards}

    @app.get("/ui/api/health")
    async def ui_health(request: Request) -> dict[str, int]:
        require_user(request)
        async with app.state.session_factory() as session:

            async def count(stmt: Any) -> int:
                return int((await session.execute(stmt)).scalar_one())

            return {
                "pending_decisions": await count(
                    select(func.count()).select_from(HITLRow).where(HITLRow.status == "pending")
                ),
                "runs_done": await count(
                    select(func.count()).select_from(RunRow).where(RunRow.status == "done")
                ),
                "runs_failed": await count(
                    select(func.count()).select_from(RunRow).where(RunRow.status == "failed")
                ),
                "vertices_running": await count(
                    select(func.count()).select_from(VertexRow).where(VertexRow.status == "running")
                ),
                "memory_episodes": await count(select(func.count()).select_from(EpisodeRow)),
            }

    @app.get("/ui/api/runs")
    async def ui_runs(request: Request, limit: int = 30) -> dict[str, Any]:
        require_user(request)
        async with app.state.session_factory() as session:
            runs = list(
                await session.scalars(select(RunRow).order_by(RunRow.id.desc()).limit(limit))
            )
            out = []
            for run in runs:
                vertices = await session.scalars(
                    select(VertexRow).where(VertexRow.run_id == run.id).order_by(VertexRow.id)
                )
                out.append(
                    {
                        "id": run.id,
                        "project": run.project,
                        "task_ref": run.task_ref,
                        "status": run.status,
                        "vertices": [
                            {
                                "id": v.id,
                                "name": v.name,
                                "status": v.status,
                                "deps": list(v.deps_json or []),
                                "leased_by": v.leased_by,
                                "attempts": v.attempts,
                                "pr_url": (v.result_json or {}).get("pr_url"),
                            }
                            for v in vertices
                        ],
                    }
                )
            return {"runs": out}

    @app.get("/ui/api/projects")
    async def ui_projects(request: Request) -> dict[str, Any]:
        require_user(request)
        path = Path(app.state.projects_file)
        registry: list[dict[str, Any]] = []
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            registry = list(data.get("projects") or [])
        async with app.state.session_factory() as session:
            counts = (
                await session.execute(
                    select(RunRow.task_ref, func.count()).group_by(RunRow.task_ref)
                )
            ).all()
        runs_by_repo: dict[str, int] = {}
        for task_ref, n in counts:
            repo = str(task_ref).split("#", 1)[0]
            runs_by_repo[repo] = runs_by_repo.get(repo, 0) + int(n)
        return {"registry": registry, "runs_by_repo": runs_by_repo}

    @app.get("/ui/api/memory")
    async def ui_memory(request: Request, q: str = "", limit: int = 50) -> dict[str, Any]:
        require_user(request)
        async with app.state.session_factory() as session:
            rows = list(
                await session.scalars(select(EpisodeRow).order_by(EpisodeRow.id.desc()).limit(500))
            )
        words = [w for w in q.lower().split() if w]
        episodes = [
            {
                "id": r.id,
                "project": r.project,
                "kind": r.kind,
                "title": r.title,
                "body": r.body[:600],
                "created_at": str(r.created_at),
            }
            for r in rows
            if not words or all(w in f"{r.title} {r.body}".lower() for w in words)
        ]
        return {"episodes": episodes[: max(1, min(limit, 200))]}
