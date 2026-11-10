"""Проектный реестр организации (v0: файл infra/projects.yaml; M10: Projects UI).

Записи: имя, тип, владелец, статус (alive|archived), дата и причина изменения.
Файл — источник правды для реестра до появления control-plane UI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

REGISTRY_PATH = Path(__file__).resolve().parents[4] / "infra" / "projects.yaml"


class ProjectRecord(BaseModel):
    name: str
    kind: str = "service"  # product | service | tool | assistant
    owner: str = ""
    status: str = "alive"  # alive | archived
    status_date: str = Field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d"))
    reason: str = ""


def load_registry(path: Path = REGISTRY_PATH) -> list[ProjectRecord]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return [ProjectRecord(**item) for item in data.get("projects", [])]


def save_registry(records: list[ProjectRecord], path: Path = REGISTRY_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"projects": [r.model_dump() for r in records]}
    path.write_text(
        "# Project registry of the organization (v0 file; M10: Projects UI).\n"
        + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    )
    return path


def register_or_update(record: ProjectRecord, path: Path = REGISTRY_PATH) -> None:
    """Добавить проект или обновить существующий по имени."""
    records = load_registry(path)
    by_name = {r.name: r for r in records}
    by_name[record.name] = record
    save_registry(list(by_name.values()), path)


def offboard_project(name: str, reason: str, path: Path = REGISTRY_PATH) -> ProjectRecord:
    """Пометить проект архивированным; исключение, если проект не найден."""
    records = load_registry(path)
    by_name = {r.name: r for r in records}
    if name not in by_name:
        raise KeyError(f"project {name!r} not in registry")
    rec = by_name[name].model_copy(
        update={
            "status": "archived",
            "status_date": datetime.now(UTC).strftime("%Y-%m-%d"),
            "reason": reason,
        }
    )
    by_name[name] = rec
    save_registry(list(by_name.values()), path)
    return rec
