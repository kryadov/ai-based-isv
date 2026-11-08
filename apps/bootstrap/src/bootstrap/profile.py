"""Профиль организации: выбор владельца, сохранение в infra/env.local.yaml (gitignored).

Профиль не содержит секретов — только решения вопросника.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

TRACKER_DEPLOY = "deploy-openproject"
TRACKER_EXISTING = "connect-existing"
LLM_HOST_OLLAMA = "host-ollama"
LLM_EXTERNAL = "external-api"


class Profile(BaseModel):
    """Решения вопросника (без секретов)."""

    target: str = Field(default="laptop-k3d", description="цель развёртывания")
    tracker: str = Field(
        default=TRACKER_DEPLOY,
        description="трекер: deploy-openproject | connect-existing",
    )
    tracker_existing_url: str = Field(
        default="", description="URL существующего трекера (если connect-existing)"
    )
    llm: str = Field(default=LLM_HOST_OLLAMA, description="модели: host-ollama | external-api")
    demo: bool = Field(default=True, description="засеять demo-репо isv-product")


def save(profile: Profile, path: Path) -> Path:
    """Сохранить профиль (создать родительский каталог)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(profile.model_dump(), sort_keys=False, allow_unicode=True))
    return path


def load(path: Path) -> Profile:
    """Загрузить профиль из файла."""
    data = yaml.safe_load(path.read_text()) or {}
    return Profile(**data)
