"""Platform deployer (M6, spec §7.2.6): merged commit → canary in prod → verdict.

1. Build ``isv-product:<sha7>`` on the host engine (k3s on Rancher Desktop shares
   the moby image store; k3d needs an import).
2. Measure the pre-rollout baseline (service 5xx ratio, p95) from Prometheus.
3. HITL gate #3: the first prod rollout of the service, or a change under
   ``deploy/**``, waits for a human.
4. Point the Argo CD Application at the image (kustomize override) with the
   baseline as post-rollout analysis args; Argo Rollouts runs the canary.
5. Watch the Rollout to a verdict: ``succeeded`` (Healthy on the new image,
   promoted to stable), ``rolled_back`` (analysis failed → aborted; the desired
   image is restored to the previous one and a postmortem draft is written) or
   ``timeout``. No verdict counts as success without a passed analysis.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel

from agent_runtime.gates import HITLPort
from agent_runtime.prom import Slo, service_slo

APP = "isv-product"
APPLICATION = "isv-product-prod"
FAULT_KEYS = ("FAULT_ERROR_RATE", "FAULT_LATENCY_MS")
# JSON pointer of the post-rollout analysis args in deploy/rollout.yaml (step 5).
_POST_ROLLOUT_ARGS = "/spec/strategy/canary/steps/5/analysis/args"

Status = Literal["succeeded", "rolled_back", "timeout", "rejected"]


class DeployError(RuntimeError):
    """The deploy could not be attempted (bad input, missing HITL port, kube error)."""


class DeployVerdict(BaseModel):
    status: Status
    image: str
    previous_image: str | None = None
    revision: str = ""
    analysis: list[dict[str, Any]] = []
    baseline: Slo
    started: str
    finished: str
    postmortem: str | None = None


class KubePort(Protocol):
    async def build(self, sha: str, repo_dir: Path) -> str: ...
    async def app_image(self) -> str | None: ...
    async def set_app_image(
        self, image: str, *, args: dict[str, str], fault_env: dict[str, str]
    ) -> None: ...
    async def rollout(self) -> dict[str, Any]: ...
    async def analysis_runs(self) -> list[dict[str, Any]]: ...
    async def has_stable(self) -> bool: ...


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _image_of(rollout: dict[str, Any]) -> str:
    containers = rollout.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    return str(containers[0].get("image", "")) if containers else ""


def _summarize(run: dict[str, Any]) -> dict[str, Any]:
    conditions = {
        m.get("name"): m.get("successCondition", "") for m in run.get("spec", {}).get("metrics", [])
    }
    metrics = []
    for result in run.get("status", {}).get("metricResults", []) or []:
        measurements = result.get("measurements", []) or []
        metrics.append(
            {
                "name": result.get("name"),
                "phase": result.get("phase"),
                "last_value": measurements[-1].get("value") if measurements else None,
                "condition": conditions.get(result.get("name"), ""),
            }
        )
    return {
        "name": run.get("metadata", {}).get("name", ""),
        "phase": run.get("status", {}).get("phase", ""),
        "metrics": metrics,
    }


def _fault_env(fault: dict[str, str] | None) -> dict[str, str]:
    env = dict.fromkeys(FAULT_KEYS, "0")
    for key, value in (fault or {}).items():
        if key not in FAULT_KEYS:
            raise DeployError(f"unknown fault {key!r} (allowed: {', '.join(FAULT_KEYS)})")
        env[key] = value
    return env


async def _await_hitl(
    hitl: HITLPort, subject: str, context: dict[str, Any], *, poll_s: float, sleep: Any
) -> str:
    request_id = await hitl.create("deploy", subject, context)
    while True:
        status = await hitl.status(request_id)
        if status in ("approved", "rejected"):
            return status
        await sleep(poll_s)


async def deploy(
    sha: str,
    repo_dir: Path,
    *,
    kube: KubePort,
    slo: Callable[[], Awaitable[Slo]],
    hitl: HITLPort | None,
    deploy_paths_changed: bool,
    fault: dict[str, str] | None = None,
    timeout_s: float = 900.0,
    poll_s: float = 10.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> DeployVerdict:
    """Roll ``sha`` out as a canary and return the metrics-backed verdict."""
    started = _now()
    env = _fault_env(fault)
    image = await kube.build(sha, repo_dir)
    baseline = await slo()
    previous = await kube.app_image()

    def verdict(status: Status, **extra: Any) -> DeployVerdict:
        return DeployVerdict(
            status=status,
            image=image,
            previous_image=previous,
            baseline=baseline,
            started=started,
            finished=_now(),
            **extra,
        )

    if deploy_paths_changed or not await kube.has_stable():
        if hitl is None:
            raise DeployError("HITL #3 required (first prod rollout or deploy/** change)")
        reason = "deploy config changed" if deploy_paths_changed else "first prod rollout"
        answer = await _await_hitl(
            hitl, image, {"image": image, "reason": reason}, poll_s=poll_s, sleep=sleep
        )
        if answer != "approved":
            return verdict("rejected")

    if previous == image and fault is None:
        current = await kube.rollout()
        if current.get("status", {}).get("phase") == "Healthy" and _image_of(current) == image:
            return verdict("succeeded", revision=_revision(current))

    args = {
        "baseline-error": f"{baseline.error_ratio:.6f}",
        "baseline-p95": f"{baseline.p95_s:.6f}",
    }
    # The verdict belongs to the NEW pod template only: until Argo CD syncs the
    # patch the Rollout still reports the old one (and for a same-image deploy,
    # e.g. a fault toggle, "Healthy on the target image" is already true).
    pre_hash = (await kube.rollout()).get("status", {}).get("currentPodHash", "")
    await kube.set_app_image(image, args=args, fault_env=env)

    elapsed = 0.0
    while elapsed < timeout_s:
        current = await kube.rollout()
        status = current.get("status", {})
        pod_hash = status.get("currentPodHash", "")
        on_new = _image_of(current) == image and bool(pod_hash) and pod_hash != pre_hash
        if on_new and (status.get("abort") is True or status.get("phase") == "Degraded"):
            runs = [_summarize(r) for r in await kube.analysis_runs()]
            if previous:
                await kube.set_app_image(previous, args=args, fault_env=_fault_env(None))
            result = verdict("rolled_back", revision=_revision(current), analysis=runs)
            result.postmortem = write_postmortem(result)
            return result
        promoted = status.get("stableRS") and status.get("stableRS") == status.get("currentPodHash")
        if on_new and status.get("phase") == "Healthy" and promoted:
            runs = [_summarize(r) for r in await kube.analysis_runs()]
            return verdict("succeeded", revision=_revision(current), analysis=runs)
        await sleep(poll_s)
        elapsed += poll_s
    return verdict("timeout")


def _revision(rollout: dict[str, Any]) -> str:
    annotations = rollout.get("metadata", {}).get("annotations", {}) or {}
    return str(annotations.get("rollout.argoproj.io/revision", ""))


async def remember_verdict(
    memory: Any,
    project: str,
    verdict: DeployVerdict,
    paths: list[str],
    *,
    run_id: int | None = None,
    vertex_id: int | None = None,
) -> None:
    """Project memory write-back of a deploy (M7): success marker or postmortem."""
    if verdict.status == "succeeded":
        await memory.add(
            project,
            "deploy",
            f"deployed {verdict.image}: succeeded (revision {verdict.revision})",
            f"Canary passed analysis; previous {verdict.previous_image}.",
            paths,
            run_id=run_id,
            vertex_id=vertex_id,
        )
    elif verdict.postmortem:
        await memory.add(
            project,
            "postmortem",
            f"canary {verdict.image} rolled back",
            verdict.postmortem,
            paths,
            run_id=run_id,
            vertex_id=vertex_id,
        )


def write_postmortem(verdict: DeployVerdict) -> str:
    """Markdown postmortem draft for a rolled-back canary (episodic memory: M7)."""
    lines = [
        "# Postmortem (draft)",
        "",
        f"- **Outcome:** canary {verdict.image} rolled back to {verdict.previous_image}",
        f"- **Window:** {verdict.started} → {verdict.finished}",
        f"- **Rollout revision:** {verdict.revision}",
        f"- **Pre-rollout baseline:** 5xx ratio {verdict.baseline.error_ratio:.4f}, "
        f"p95 {verdict.baseline.p95_s:.3f}s",
        "",
        "## Failed analysis",
        "",
    ]
    failed = [r for r in verdict.analysis if r.get("phase") != "Successful"]
    for run in failed or verdict.analysis:
        lines.append(f"- AnalysisRun `{run.get('name')}`: {run.get('phase')}")
        for metric in run.get("metrics", []):
            lines.append(
                f"  - `{metric.get('name')}`: {metric.get('phase')} "
                f"(last value {metric.get('last_value')}, condition `{metric.get('condition')}`)"
            )
    lines += [
        "",
        "## Next steps",
        "",
        "- Reproduce the failing metric against the canary image before re-deploying.",
        "- Link the fix PR here; record the lesson in project memory (M7).",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- kubectl


def application_patch(
    image: str, *, args: dict[str, str], fault_env: dict[str, str]
) -> dict[str, Any]:
    """Merge patch for the Argo CD Application: image + Rollout JSON6902 patch."""
    ops: list[dict[str, str]] = []
    for index, name in enumerate(("baseline-error", "baseline-p95")):
        ops.append(
            {
                "op": "replace",
                "path": f"{_POST_ROLLOUT_ARGS}/{index}/value",
                "value": args[name],
            }
        )
    for index, name in enumerate(FAULT_KEYS):
        ops.append(
            {
                "op": "replace",
                "path": f"/spec/template/spec/containers/0/env/{index}/value",
                "value": fault_env[name],
            }
        )
    return {
        "spec": {
            "source": {
                "kustomize": {
                    "images": [f"{APP}={image}"],
                    "patches": [
                        {
                            "target": {"group": "argoproj.io", "kind": "Rollout", "name": APP},
                            "patch": json.dumps(ops),
                        }
                    ],
                }
            }
        }
    }


class KubectlPort:
    """KubePort over kubectl/docker on the stand (run inside the toolbox)."""

    def __init__(self, *, namespace: str = "prod", argocd_namespace: str = "platform") -> None:
        self._ns = namespace
        self._argo_ns = argocd_namespace

    @staticmethod
    def _run(argv: list[str], *, stdin: str | None = None) -> str:
        done = subprocess.run(  # noqa: S603
            argv, input=stdin, capture_output=True, text=True, check=False
        )
        if done.returncode != 0:
            raise DeployError(f"{argv[0]} {argv[1]} failed: {done.stderr.strip()[:300]}")
        return done.stdout

    async def _json(self, argv: list[str]) -> dict[str, Any]:
        out = await asyncio.to_thread(self._run, argv)
        data: dict[str, Any] = json.loads(out)
        return data

    async def build(self, sha: str, repo_dir: Path) -> str:
        image = f"{APP}:{sha[:7]}"
        await asyncio.to_thread(self._run, ["docker", "build", "-q", "-t", image, str(repo_dir)])
        if os.environ.get("SWARM_CLUSTER", "k3d") == "k3d":
            await asyncio.to_thread(self._run, ["k3d", "image", "import", image, "-c", "swarm-lab"])
        return image

    async def app_image(self) -> str | None:
        app = await self._json(
            ["kubectl", "-n", self._argo_ns, "get", "application", APPLICATION, "-o", "json"]
        )
        for entry in app.get("spec", {}).get("source", {}).get("kustomize", {}).get("images", []):
            name, _, target = str(entry).partition("=")
            if name == APP and target:
                return target
        return _image_of(await self.rollout()) or None

    async def set_app_image(
        self, image: str, *, args: dict[str, str], fault_env: dict[str, str]
    ) -> None:
        patch = json.dumps(application_patch(image, args=args, fault_env=fault_env))
        await asyncio.to_thread(
            self._run,
            [
                "kubectl", "-n", self._argo_ns, "patch", "application", APPLICATION,
                "--type", "merge", "-p", patch,
            ],
        )  # fmt: skip

    async def rollout(self) -> dict[str, Any]:
        return await self._json(["kubectl", "-n", self._ns, "get", "rollout", APP, "-o", "json"])

    async def has_stable(self) -> bool:
        return bool((await self.rollout()).get("status", {}).get("stableRS"))

    async def analysis_runs(self) -> list[dict[str, Any]]:
        current = (await self.rollout()).get("status", {}).get("currentPodHash", "")
        data = await self._json(
            [
                "kubectl", "-n", self._ns, "get", "analysisrun",
                "-l", f"rollouts-pod-template-hash={current}", "-o", "json",
            ]
        )  # fmt: skip
        items: list[dict[str, Any]] = data.get("items", [])
        return items


# --------------------------------------------------------------------------- CLI


def _parse_fault(items: list[str]) -> dict[str, str] | None:
    if not items:
        return None
    fault: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise DeployError(f"--fault expects KEY=VALUE, got {item!r}")
        fault[key] = value
    return fault


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy a commit as a canary (M6).")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--repo-dir", required=True, type=Path)
    parser.add_argument("--fault", action="append", default=[], help="FAULT_ERROR_RATE=0.5")
    parser.add_argument("--prometheus", default="http://localhost:30090")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--memory-api", help="agent_api URL: write the verdict to project memory")
    parser.add_argument("--project", default="swarm-lab/isv-product")
    ns = parser.parse_args(argv)
    async with httpx.AsyncClient(timeout=30.0) as http:

        async def slo() -> Slo:
            return await service_slo(http, ns.prometheus, namespace="prod", service=APP)

        result = await deploy(
            ns.sha,
            ns.repo_dir,
            kube=KubectlPort(),
            slo=slo,
            hitl=None,
            deploy_paths_changed=False,
            fault=_parse_fault(ns.fault),
            timeout_s=ns.timeout,
        )
    if ns.memory_api:
        from agent_runtime.memory import MemoryClient

        # The episode's paths: what the deployed commit changed (vs its first parent).
        changed = subprocess.run(  # noqa: S603, S607
            ["git", "-C", str(ns.repo_dir), "diff", "--name-only", "HEAD~1", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        async with httpx.AsyncClient(timeout=30.0) as http:
            await remember_verdict(MemoryClient(http, ns.memory_api), ns.project, result, changed)
    if ns.evidence_dir:
        ns.evidence_dir.mkdir(parents=True, exist_ok=True)
        (ns.evidence_dir / "deploy_verdict.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        if result.postmortem:
            (ns.evidence_dir / "postmortem.md").write_text(result.postmortem, encoding="utf-8")
    print(result.model_dump_json(indent=2))
    return 0 if result.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
