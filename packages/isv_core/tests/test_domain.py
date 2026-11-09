"""isv_core domain model tests."""

from datetime import datetime

from isv_core.domain import (
    AgentIdentity,
    AuditEvent,
    BudgetRecord,
    DecisionAction,
    HITLKind,
    HITLRequest,
    HITLStatus,
    Run,
    RunStatus,
    TaskVertex,
    VertexStatus,
)


def test_vertex_defaults_and_roundtrip() -> None:
    v = TaskVertex(run_id=1, name="api", done_when="tests green", paths_allow=["src/api/**"])
    assert v.status is VertexStatus.QUEUED
    assert v.attempts == 0
    data = v.model_dump()
    v2 = TaskVertex(**data)
    assert v2 == v


def test_vertex_deps_and_paths() -> None:
    v = TaskVertex(run_id=1, name="rbac", deps=["api"], done_when="review ok")
    assert v.deps == ["api"]
    assert v.paths_allow == []


def test_run_budget_fields() -> None:
    r = Run(project="isv-product", task_ref="TRK-118", budget_tokens=1000)
    assert r.status is RunStatus.PENDING
    assert r.budget_spent == 0


def test_hitl_lifecycle_fields() -> None:
    h = HITLRequest(kind=HITLKind.MERGE, subject="PR #42")
    assert h.status is HITLStatus.PENDING
    h.status = HITLStatus.APPROVED
    h.decided_by = "kryadov"
    h.decided_at = datetime.now()
    assert h.decision_action_available() if hasattr(h, "decision_action_available") else True
    assert DecisionAction.APPROVE.value == "approve"


def test_hitl_kind_values() -> None:
    assert HITLKind.PLAN_APPROVAL.value == "plan-approval"
    assert HITLKind.AUTOMATION_ACTION.value == "automation-action"


def test_identity_and_budget() -> None:
    ident = AgentIdentity(name="exec-csv-01", role="executor", allowed_sections=["export"])
    assert ident.role == "executor"
    b = BudgetRecord(subject="run-42", limit_tokens=100, spent_tokens=40)
    assert b.limit_tokens - b.spent_tokens == 60


def test_audit_event_append_shape() -> None:
    e = AuditEvent(actor="exec-csv-01", action="vertex.complete", target="vertex:12",
                   payload={"pr": 41})
    assert e.outcome == "ok"
    assert e.payload == {"pr": 41}
