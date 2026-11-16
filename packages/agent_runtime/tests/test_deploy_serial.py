"""Deploys of one app are serialized (M11, found live): two vertices of a swarm run
merged ~90 s apart, both canaries ran at once and one verdict timed out waiting
for its pod-template-hash while the other rollout replaced it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agent_runtime.worker_deploy import already_live, deploy_lock


async def test_deploy_lock_serializes_holders(tmp_path: Path) -> None:
    order: list[str] = []

    async def holder(name: str, hold: float) -> None:
        async with deploy_lock("isv-product", tmp_path):
            order.append(f"{name}+")
            await asyncio.sleep(hold)
            order.append(f"{name}-")

    first = asyncio.create_task(holder("a", 0.3))
    await asyncio.sleep(0.05)
    await asyncio.gather(first, holder("b", 0.0))
    assert order == ["a+", "a-", "b+", "b-"]


class FakeKube:
    def __init__(self, image: str, phase: str) -> None:
        self.image, self.phase = image, phase

    async def rollout(self) -> dict[str, Any]:
        return {
            "spec": {"template": {"spec": {"containers": [{"image": self.image}]}}},
            "status": {"phase": self.phase},
        }


async def test_already_live_when_the_commit_is_the_healthy_image() -> None:
    sha = "b2fd53e0123456789"
    assert await already_live(FakeKube("isv-product:b2fd53e", "Healthy"), sha)
    assert not await already_live(FakeKube("isv-product:aaf4518", "Healthy"), sha)
    assert not await already_live(FakeKube("isv-product:b2fd53e", "Progressing"), sha)
