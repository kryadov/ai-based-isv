"""FastAPI control plane (spec §7.3): queue, identities, budgets, HITL, audit."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent_api import store
from agent_api.automations import register_automations
from agent_api.models import AuditRow, Base, HITLRow, RunRow, VertexRow
from agent_api.settings import Settings, settings
from agent_api.ui import register_ui
from agent_api.ui_auth import COOKIE, parse_users, verify


def create_engine_session() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    url = getattr(app.state, "override_database_url", None) or settings.database_url
    engine = create_async_engine(url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.state.session_factory = factory
    app.state.engine = engine
    yield
    await engine.dispose()


# Paths outside the service-token guard: liveness, the Prometheus scrape
# (aggregates only) and the panel, which has its own human login (M10).
OPEN_PATHS = ("/healthz", "/metrics")
PANEL_PREFIX = "/ui"


def _panel_user(app: FastAPI, request: Request) -> str | None:
    session = request.cookies.get(COOKIE, "")
    user = verify(session, app.state.session_secret) if session else None
    return user if user and user in app.state.ui_users else None


OPERATOR = "operator"  # the identity that may decide HITL requests (M12)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def parse_service_tokens(spec: str) -> dict[str, str]:
    """``name:sha256hex,...`` → {sha256hex: name}; malformed entries are skipped (M12).

    Only hashes are configured, so the Deployment's Secret never holds a usable token.
    """
    out: dict[str, str] = {}
    for entry in spec.split(","):
        name, _, digest = entry.strip().partition(":")
        digest = digest.strip().lower()
        if name.strip() and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            out[digest] = name.strip()
    return out


def _identity_of(header: str, identities: dict[str, str]) -> str | None:
    """The identity whose token is in ``Authorization: Bearer <token>``, else None.

    Every configured hash is compared (constant time) so the timing does not tell
    which identity — or whether any — matched.
    """
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    given = _sha256(token).encode()
    found: str | None = None
    for digest, name in identities.items():
        if hmac.compare_digest(given, digest.encode()):
            found = name
    return found


def _guard_service_api(app: FastAPI, identities: dict[str, str]) -> None:
    """Require a known bearer token on every service endpoint (M11; per identity M12).

    ``request.state.identity`` carries who called: an identity name, ``panel:<user>``
    for a logged-in human reading a listing, or ``""`` when no token is configured.
    """
    app.state.service_auth = bool(identities)

    @app.middleware("http")
    async def service_token(request: Request, call_next: Any) -> Response:
        request.state.identity = ""
        path = request.url.path
        if (
            not identities
            or path in OPEN_PATHS
            or path == PANEL_PREFIX
            or path.startswith(PANEL_PREFIX + "/")
        ):
            response: Response = await call_next(request)
            return response
        identity = _identity_of(request.headers.get("authorization", ""), identities)
        if identity is None and request.method == "GET":
            # The panel reads a few service listings (automations); a logged-in
            # human may READ them, while actions go through /ui/api (decided_by).
            user = _panel_user(app, request)
            identity = f"panel:{user}" if user else None
        if identity is None:
            return JSONResponse({"detail": "service token required"}, status_code=401)
        request.state.identity = identity
        response = await call_next(request)
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="agent_api", lifespan=lifespan)
    current = Settings()
    app.state.ui_users = parse_users(current.ui_users)
    app.state.session_secret = current.session_secret
    app.state.projects_file = current.projects_file
    app.state.automations_dir = current.automations_dir
    register_automations(app)
    register_ui(app)
    identities = parse_service_tokens(current.service_tokens)
    if current.service_token:  # the M11 single token stays valid as the operator
        identities[_sha256(current.service_token)] = OPERATOR
    _guard_service_api(app, identities)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs")
    async def create_run(payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            run_id = await store.create_run(
                session,
                project=str(payload.get("project", "")),
                task_ref=str(payload.get("task_ref", "")),
                budget_tokens=int(payload.get("budget_tokens", 200_000)),
            )
            return {"run_id": run_id}

    @app.get("/runs")
    async def get_runs(
        task_ref: str = "", project: str = "", status: str = "", limit: int = 50
    ) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            runs = await store.list_runs(
                session, task_ref=task_ref, project=project, status=status, limit=limit
            )
        return {"runs": runs}

    @app.post("/runs/{run_id}/vertices")
    async def add_vertex(run_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            vertex_id = await store.add_vertex(
                session,
                run_id,
                name=str(payload.get("name", "")),
                done_when=str(payload.get("done_when", "")),
                deps=list(payload.get("deps", [])),
                paths_allow=list(payload.get("paths_allow", [])),
            )
            return {"vertex_id": vertex_id}

    # --- swarm (M8): the run graph, the judge verdict, metrics -------------------

    @app.get("/runs/{run_id}/vertices")
    async def run_vertices(run_id: int) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            rows = await session.scalars(
                select(VertexRow).where(VertexRow.run_id == run_id).order_by(VertexRow.id)
            )
            return {
                "vertices": [
                    {
                        "id": r.id,
                        "name": r.name,
                        "status": r.status,
                        "deps": list(r.deps_json or []),
                        "paths_allow": list(r.paths_allow_json or []),
                        "attempts": r.attempts,
                        "leased_by": r.leased_by,
                        "result": r.result_json or {},
                    }
                    for r in rows
                ]
            }

    @app.post("/runs/{run_id}/status")
    async def set_run_status(run_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status", ""))
        if status not in ("done", "failed"):
            raise HTTPException(status_code=422, detail="status must be done or failed")
        async with app.state.session_factory() as session:
            run = (await session.scalars(select(RunRow).where(RunRow.id == run_id))).first()
            if run is None:
                raise HTTPException(status_code=404, detail="run not found")
            run.status = status
            await session.commit()
            await store.append_audit(
                session,
                actor=str(payload.get("actor", "judge")),
                action="run_status",
                target=f"run:{run_id}",
                outcome="ok" if status == "done" else "error",
                payload={"status": status, "note": str(payload.get("note", ""))},
            )
        return {"run_id": run_id, "status": status}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> str:
        async with app.state.session_factory() as session:
            vertices = (
                await session.execute(
                    select(VertexRow.status, func.count()).group_by(VertexRow.status)
                )
            ).all()
            hitl = (
                await session.execute(
                    select(HITLRow.kind, func.count())
                    .where(HITLRow.status == "pending")
                    .group_by(HITLRow.kind)
                )
            ).all()
            runs = (
                await session.execute(select(RunRow.status, func.count()).group_by(RunRow.status))
            ).all()
        lines = [
            "# HELP swarm_vertices Vertices by status (all runs).",
            "# TYPE swarm_vertices gauge",
            *(f'swarm_vertices{{status="{s}"}} {n}' for s, n in vertices),
            "# HELP swarm_hitl_pending Pending HITL requests by kind.",
            "# TYPE swarm_hitl_pending gauge",
            *(f'swarm_hitl_pending{{kind="{k}"}} {n}' for k, n in hitl),
            "# HELP swarm_runs Runs by status.",
            "# TYPE swarm_runs gauge",
            *(f'swarm_runs{{status="{s}"}} {n}' for s, n in runs),
        ]
        return "\n".join(lines) + "\n"

    @app.post("/vertices/claim")
    async def claim_vertex(payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            claimed = await store.claim_vertex(
                session,
                run_id=int(payload["run_id"]),
                agent=str(payload.get("agent", "worker")),
                ttl_seconds=int(payload.get("ttl_seconds", 300)),
            )
            return {"vertex": claimed}

    @app.post("/vertices/{vertex_id}/heartbeat")
    async def heartbeat(vertex_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            ok = await store.heartbeat_vertex(
                session,
                vertex_id,
                agent=str(payload.get("agent", "")),
                ttl_seconds=int(payload.get("ttl_seconds", 300)),
            )
            if not ok:
                raise HTTPException(status_code=409, detail="vertex not leased by agent")
            return {"ok": True}

    @app.post("/vertices/{vertex_id}/complete")
    async def complete(vertex_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            ok = await store.complete_vertex(
                session,
                vertex_id,
                agent=str(payload.get("agent", "")),
                result_data=payload.get("result"),
            )
            if not ok:
                raise HTTPException(status_code=409, detail="vertex not leased by agent")
            return {"ok": True}

    @app.post("/vertices/{vertex_id}/fail")
    async def fail(vertex_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            terminal = payload.get("terminal") is True
            ok = await store.fail_vertex(
                session, vertex_id, agent=str(payload.get("agent", "")), terminal=terminal
            )
            if not ok:
                raise HTTPException(status_code=409, detail="vertex not leased by agent")
            return {"ok": True, "requeued": not terminal}

    @app.post("/identities")
    async def register_identity(payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            ident_id = await store.register_identity(
                session,
                name=str(payload["name"]),
                role=str(payload.get("role", "executor")),
                allowed_sections=list(payload.get("allowed_sections", [])),
            )
            return {"identity_id": ident_id}

    @app.post("/budgets/{subject}")
    async def set_budget(subject: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            budget_id = await store.set_budget(
                session, subject, limit_tokens=int(payload.get("limit_tokens", 0))
            )
            return {"budget_id": budget_id}

    @app.post("/budgets/{subject}/consume")
    async def consume_budget(subject: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            ok = await store.consume_budget(session, subject, tokens=int(payload.get("tokens", 0)))
            if not ok:
                raise HTTPException(status_code=402, detail="budget exceeded")
            return {"ok": True}

    @app.post("/hitl")
    async def create_hitl(payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            rid = await store.create_hitl(
                session,
                kind=str(payload["kind"]),
                subject=str(payload["subject"]),
                context=payload.get("context"),
            )
            return {"request_id": rid}

    @app.get("/hitl/pending")
    async def list_pending() -> dict[str, Any]:
        async with app.state.session_factory() as session:
            rows = await session.scalars(select(HITLRow).where(HITLRow.status == "pending"))
            return {"pending": [{"id": r.id, "kind": r.kind, "subject": r.subject} for r in rows]}

    @app.get("/hitl/{request_id}")
    async def get_hitl(request_id: int) -> dict[str, Any]:
        # The gate corridor (M5) polls its merge request here.
        async with app.state.session_factory() as session:
            row = (await session.scalars(select(HITLRow).where(HITLRow.id == request_id))).first()
            if row is None:
                raise HTTPException(status_code=404, detail="not found")
            return {
                "id": row.id,
                "kind": row.kind,
                "subject": row.subject,
                "status": row.status,
                "context": row.context_json or {},
            }

    @app.post("/hitl/{request_id}/decision")
    async def decide_hitl(
        request_id: int, payload: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        # M12: with service auth on, only the operator decides — a worker, the
        # daemon or the poller can never approve its own merge.
        if app.state.service_auth and request.state.identity != OPERATOR:
            raise HTTPException(status_code=403, detail="only the operator decides HITL")
        async with app.state.session_factory() as session:
            from sqlalchemy import select

            status = await store.decide_hitl(
                session,
                request_id,
                action=str(payload.get("action", "approve")),
                decided_by=str(payload.get("decided_by", "human")),
                note=payload.get("note"),
            )
            if status is None:
                rows = await session.scalars(select(HITLRow).where(HITLRow.id == request_id))
                exists = rows.first() is not None
                raise HTTPException(
                    status_code=409 if exists else 404,
                    detail="not pending" if exists else "not found",
                )
            return {"status": status}

    @app.post("/audit")
    async def append_audit(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        body = payload.get("payload")
        identity = request.state.identity
        if identity:  # M12: who really called, next to the self-reported actor
            base = body if isinstance(body, dict) else ({} if body is None else {"value": body})
            body = {**base, "identity": identity}
        async with app.state.session_factory() as session:
            event_id = await store.append_audit(
                session,
                actor=str(payload["actor"]),
                action=str(payload["action"]),
                target=str(payload["target"]),
                outcome=str(payload.get("outcome", "ok")),
                payload=body,
            )
            return {"event_id": event_id}

    @app.get("/audit")
    async def list_audit(target: str | None = None, limit: int = 50) -> dict[str, Any]:
        from sqlalchemy import select

        async with app.state.session_factory() as session:
            stmt = select(AuditRow).order_by(AuditRow.id.desc()).limit(limit)
            if target:
                stmt = stmt.where(AuditRow.target == target)
            rows = await session.scalars(stmt)
            return {
                "events": [
                    {
                        "id": r.id,
                        "ts": str(r.ts),
                        "actor": r.actor,
                        "action": r.action,
                        "target": r.target,
                        "outcome": r.outcome,
                        "payload": r.payload_json,
                    }
                    for r in rows
                ]
            }

    @app.post("/merge-queue")
    async def enqueue_merge(payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            entry_id = await store.enqueue_merge(
                session,
                pr_ref=str(payload["pr_ref"]),
                project=str(payload["project"]),
            )
            return {"entry_id": entry_id}

    # --- project episodic memory (M7, ADR-0003) -------------------------------

    @app.post("/memory/episodes")
    async def add_episode(payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind", ""))
        if kind not in store.EPISODE_KINDS:
            raise HTTPException(status_code=422, detail=f"unknown episode kind {kind!r}")
        project = str(payload.get("project", "")).strip()
        if not project:
            raise HTTPException(status_code=422, detail="project is required")

        def _opt_int(key: str) -> int | None:
            value = payload.get(key)
            return int(value) if isinstance(value, int) else None

        async with app.state.session_factory() as session:
            episode_id = await store.add_episode(
                session,
                project=project,
                kind=kind,
                title=str(payload.get("title", "")),
                body=str(payload.get("body", "")),
                paths=list(payload.get("paths") or []),
                tags=list(payload.get("tags") or []),
                run_id=_opt_int("run_id"),
                vertex_id=_opt_int("vertex_id"),
            )
            return {"id": episode_id}

    @app.get("/memory/episodes/{episode_id}")
    async def get_episode(episode_id: int) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            episode = await store.get_episode(session, episode_id)
        if episode is None:
            raise HTTPException(status_code=404, detail="not found")
        return episode

    @app.get("/memory/slice")
    async def memory_slice(
        project: str, paths: str = "", q: str = "", limit: int = 5
    ) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            episodes = await store.list_project_episodes(session, project)
        ranked = store.rank_episodes(
            episodes,
            paths=[p for p in paths.split(",") if p],
            text=q,
            now=datetime.now(UTC),
            limit=limit,
        )
        return {"episodes": ranked}

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("agent_api.main:app", port=8000)
