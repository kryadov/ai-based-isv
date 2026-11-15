"""Deployer: merged commit → canary rollout → verdict (M6)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from agent_runtime.deployer import (
    DeployError,
    application_patch,
    deploy,
    write_postmortem,
)
from agent_runtime.prom import Slo

NEW = "isv-product:abc1234"
OLD = "isv-product:seed"


def _rollout(
    image: str, phase: str, *, abort: bool = False, stable: str = "s1", pod_hash: str = ""
) -> dict[str, Any]:
    current = pod_hash or ("c2" if image == NEW else "s1")
    return {
        "metadata": {"annotations": {"rollout.argoproj.io/revision": "7"}},
        "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
        "status": {
            "phase": phase,
            "abort": abort,
            "stableRS": stable,
            "currentPodHash": current,
        },
    }


class FakeKube:
    def __init__(self, phases: list[dict[str, Any]], *, image: str = OLD, stable: bool = True):
        self.phases = phases
        self.image = image
        self.stable = stable
        self.set_calls: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.runs: list[dict[str, Any]] = []

    async def build(self, sha: str, repo_dir: Path) -> str:
        return f"isv-product:{sha[:7]}"

    async def app_image(self) -> str | None:
        return self.image

    async def set_app_image(
        self, image: str, *, args: dict[str, str], fault_env: dict[str, str]
    ) -> None:
        self.set_calls.append((image, args, fault_env))
        self.image = image

    async def rollout(self) -> dict[str, Any]:
        return self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]

    async def analysis_runs(self) -> list[dict[str, Any]]:
        return self.runs

    async def has_stable(self) -> bool:
        return self.stable


class FakeHITL:
    def __init__(self, answer: str = "approved") -> None:
        self.answer = answer
        self.created: list[tuple[str, str, dict[str, Any]]] = []

    async def create(self, kind: str, subject: str, context: dict[str, Any]) -> int:
        self.created.append((kind, subject, context))
        return 5

    async def status(self, request_id: int) -> str:
        return self.answer


async def _slo() -> Slo:
    return Slo(error_ratio=0.01, p95_s=0.12)


async def _instant(_: float) -> None:
    await asyncio.sleep(0)


async def _deploy(kube: FakeKube, **kw: Any) -> Any:
    kw.setdefault("hitl", FakeHITL())
    kw.setdefault("deploy_paths_changed", False)
    return await deploy(
        "abc1234def",
        Path("."),
        kube=kube,
        slo=_slo,
        timeout_s=5,
        poll_s=1,
        sleep=_instant,
        **kw,
    )


def _failed_run() -> dict[str, Any]:
    return {
        "metadata": {"name": "isv-product-c2-7"},
        "status": {
            "phase": "Failed",
            "metricResults": [
                {
                    "name": "error-ratio",
                    "phase": "Failed",
                    "measurements": [{"phase": "Failed", "value": "[0]"}],
                }
            ],
        },
        "spec": {"metrics": [{"name": "error-ratio", "successCondition": "result[0] == 1"}]},
    }


async def test_success_marks_succeeded() -> None:
    kube = FakeKube(
        [
            _rollout(OLD, "Healthy"),
            _rollout(NEW, "Progressing"),
            _rollout(NEW, "Healthy", stable="c2"),
        ]
    )
    kube.runs = [{"metadata": {"name": "r"}, "status": {"phase": "Successful"}}]
    verdict = await _deploy(kube)
    assert verdict.status == "succeeded"
    assert verdict.image == NEW and verdict.revision == "7"
    assert verdict.analysis[0]["phase"] == "Successful"
    assert verdict.postmortem is None


async def test_old_healthy_before_sync_is_not_success() -> None:
    # Right after the patch Argo CD has not synced yet: Healthy on the OLD image
    # must never count as the new version being live.
    kube = FakeKube([_rollout(OLD, "Healthy")])
    verdict = await _deploy(kube)
    assert verdict.status == "timeout"


async def test_degraded_rolls_back_with_postmortem() -> None:
    kube = FakeKube(
        [
            _rollout(OLD, "Healthy"),
            _rollout(NEW, "Progressing"),
            _rollout(NEW, "Degraded", abort=True),
        ]
    )
    kube.runs = [_failed_run()]
    verdict = await _deploy(kube)
    assert verdict.status == "rolled_back"
    # the desired state is restored to the previous image (Argo CD stops fighting)
    assert [c[0] for c in kube.set_calls] == [NEW, OLD]
    assert kube.set_calls[1][2] == {"FAULT_ERROR_RATE": "0", "FAULT_LATENCY_MS": "0"}
    assert verdict.postmortem is not None
    assert "error-ratio" in verdict.postmortem and "result[0] == 1" in verdict.postmortem
    assert OLD in verdict.postmortem and NEW in verdict.postmortem


async def test_timeout_leaves_rollout() -> None:
    kube = FakeKube([_rollout(NEW, "Progressing")])
    verdict = await _deploy(kube)
    assert verdict.status == "timeout"
    assert [c[0] for c in kube.set_calls] == [NEW]


async def test_first_rollout_requires_hitl() -> None:
    kube = FakeKube([_rollout(OLD, "Healthy"), _rollout(NEW, "Healthy", stable="c2")], stable=False)
    hitl = FakeHITL("rejected")
    verdict = await _deploy(kube, hitl=hitl)
    assert verdict.status == "rejected"
    assert hitl.created[0][0] == "deploy"
    assert kube.set_calls == []


async def test_deploy_paths_changed_requires_hitl() -> None:
    kube = FakeKube([_rollout(OLD, "Healthy"), _rollout(NEW, "Healthy", stable="c2")])
    hitl = FakeHITL("approved")
    verdict = await _deploy(kube, hitl=hitl, deploy_paths_changed=True)
    assert verdict.status == "succeeded"
    assert [c[0] for c in hitl.created] == ["deploy"]


async def test_hitl_needed_without_port_raises() -> None:
    kube = FakeKube([_rollout(NEW, "Healthy")], stable=False)
    with pytest.raises(DeployError, match="HITL"):
        await _deploy(kube, hitl=None)


async def test_same_image_is_noop_success() -> None:
    kube = FakeKube([_rollout(NEW, "Healthy", stable="c2")], image=NEW)
    verdict = await _deploy(kube)
    assert verdict.status == "succeeded"
    assert kube.set_calls == []


async def test_fault_env_and_baseline_args_passed() -> None:
    kube = FakeKube([_rollout(OLD, "Healthy"), _rollout(NEW, "Healthy", stable="c2")])
    await _deploy(kube, fault={"FAULT_ERROR_RATE": "0.5"})
    image, args, env = kube.set_calls[0]
    assert image == NEW
    assert args == {"baseline-error": "0.010000", "baseline-p95": "0.120000"}
    assert env == {"FAULT_ERROR_RATE": "0.5", "FAULT_LATENCY_MS": "0"}


async def test_unknown_fault_key_rejected() -> None:
    kube = FakeKube([_rollout(NEW, "Healthy")])
    with pytest.raises(DeployError, match="fault"):
        await _deploy(kube, fault={"PATH": "x"})


def test_application_patch_shape() -> None:
    patch = application_patch(
        NEW,
        args={"baseline-error": "0.01", "baseline-p95": "0.2"},
        fault_env={"FAULT_ERROR_RATE": "0.5", "FAULT_LATENCY_MS": "0"},
    )
    kustomize = patch["spec"]["source"]["kustomize"]
    assert kustomize["images"] == [f"isv-product={NEW}"]
    target = kustomize["patches"][0]["target"]
    assert target == {"group": "argoproj.io", "kind": "Rollout", "name": "isv-product"}
    ops = kustomize["patches"][0]["patch"]
    assert '"/spec/strategy/canary/steps/5/analysis/args/0/value"' in ops
    assert '"0.5"' in ops and '"0.2"' in ops


def test_postmortem_for_success_is_empty_string_free() -> None:
    from agent_runtime.deployer import DeployVerdict

    verdict = DeployVerdict(
        status="rolled_back",
        image=NEW,
        previous_image=OLD,
        revision="7",
        analysis=[{"name": "r", "phase": "Failed", "metrics": []}],
        baseline=Slo(error_ratio=0.0, p95_s=0.0),
        started="t0",
        finished="t1",
    )
    text = write_postmortem(verdict)
    assert text.startswith("# Postmortem (draft)")
    assert "rolled back" in text


async def test_same_image_fault_waits_for_the_new_template() -> None:
    # Live bug (M6): a fault deploy keeps the image, so "Healthy on the target
    # image" is already true before Argo CD syncs. Only a NEW pod template counts.
    kube = FakeKube(
        [
            _rollout(NEW, "Healthy", stable="c2", pod_hash="c2"),  # before the patch
            _rollout(NEW, "Healthy", stable="c2", pod_hash="c2"),  # not synced yet
            _rollout(NEW, "Progressing", stable="c2", pod_hash="f3"),
            _rollout(NEW, "Degraded", abort=True, stable="c2", pod_hash="f3"),
        ],
        image=NEW,
    )
    kube.runs = [_failed_run()]
    verdict = await _deploy(kube, fault={"FAULT_ERROR_RATE": "0.5"})
    assert verdict.status == "rolled_back"
    # restore: same image, faults cleared
    assert kube.set_calls[-1][0] == NEW
    assert kube.set_calls[-1][2] == {"FAULT_ERROR_RATE": "0", "FAULT_LATENCY_MS": "0"}
