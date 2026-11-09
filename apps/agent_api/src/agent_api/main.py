"""FastAPI control plane (спека §7.3): очередь, идентичности, бюджеты, HITL, audit."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent_api import store
from agent_api.models import AuditRow, Base, HITLRow
from agent_api.settings import settings


def create_engine_session() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine, factory = create_engine_session()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app.state.session_factory = factory
    app.state.engine = engine
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="agent_api", lifespan=lifespan)

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

    @app.post("/runs/{run_id}/vertices")
    async def add_vertex(run_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            vertex_id = await store.add_vertex(
                session, run_id,
                name=str(payload.get("name", "")),
                done_when=str(payload.get("done_when", "")),
                deps=list(payload.get("deps", [])),
                paths_allow=list(payload.get("paths_allow", [])),
            )
            return {"vertex_id": vertex_id}

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
                session, vertex_id, agent=str(payload.get("agent", "")),
                ttl_seconds=int(payload.get("ttl_seconds", 300)),
            )
            if not ok:
                raise HTTPException(status_code=409, detail="vertex not leased by agent")
            return {"ok": True}

    @app.post("/vertices/{vertex_id}/complete")
    async def complete(vertex_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            ok = await store.complete_vertex(
                session, vertex_id, agent=str(payload.get("agent", "")),
                result_data=payload.get("result"),
            )
            if not ok:
                raise HTTPException(status_code=409, detail="vertex not leased by agent")
            return {"ok": True}

    @app.post("/vertices/{vertex_id}/fail")
    async def fail(vertex_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            ok = await store.fail_vertex(session, vertex_id, agent=str(payload.get("agent", "")))
            if not ok:
                raise HTTPException(status_code=409, detail="vertex not leased by agent")
            return {"ok": True, "requeued": True}

    @app.post("/identities")
    async def register_identity(payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            ident_id = await store.register_identity(
                session, name=str(payload["name"]), role=str(payload.get("role", "executor")),
                allowed_sections=list(payload.get("allowed_sections", [])),
            )
            return {"identity_id": ident_id}

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
                session, kind=str(payload["kind"]), subject=str(payload["subject"]),
                context=payload.get("context"),
            )
            return {"request_id": rid}

    @app.get("/hitl/pending")
    async def list_pending() -> dict[str, Any]:
        async with app.state.session_factory() as session:
            rows = await session.scalars(
                select(HITLRow).where(HITLRow.status == "pending")
            )
            return {"pending": [
                {"id": r.id, "kind": r.kind, "subject": r.subject} for r in rows
            ]}

    @app.post("/hitl/{request_id}/decision")
    async def decide_hitl(request_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            from sqlalchemy import select
            status = await store.decide_hitl(
                session, request_id,
                action=str(payload.get("action", "approve")),
                decided_by=str(payload.get("decided_by", "human")),
                note=payload.get("note"),
            )
            if status is None:
                rows = await session.scalars(select(HITLRow).where(HITLRow.id == request_id))
                exists = rows.first() is not None
                raise HTTPException(status_code=409 if exists else 404,
                                    detail="not pending" if exists else "not found")
            return {"status": status}

    @app.post("/audit")
    async def append_audit(payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            event_id = await store.append_audit(
                session, actor=str(payload["actor"]), action=str(payload["action"]),
                target=str(payload["target"]), outcome=str(payload.get("outcome", "ok")),
                payload=payload.get("payload"),
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
            return {"events": [
                {"id": r.id, "ts": str(r.ts), "actor": r.actor, "action": r.action,
                 "target": r.target, "outcome": r.outcome, "payload": r.payload_json}
                for r in rows
            ]}

    @app.post("/merge-queue")
    async def enqueue_merge(payload: dict[str, Any]) -> dict[str, Any]:
        async with app.state.session_factory() as session:
            entry_id = await store.enqueue_merge(
                session, pr_ref=str(payload["pr_ref"]), project=str(payload["project"]),
            )
            return {"entry_id": entry_id}

    return app






if __name__ == "__main__":
    import uvicorn

    uvicorn.run("agent_api.main:app", port=8000)
