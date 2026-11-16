"""Per-project intake config, ``.platform/project.yaml`` (M11).

The dispatcher reads it from every repo of the org: ``intake`` opts the repo
into issue pickup, ``paths`` bounds what the planner may hand out, and
``done_when`` is the platform-supplied acceptance command (never the
planner's). ``.platform/**`` is a protected gate path, so agents cannot edit
their own acceptance.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class DeployTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str = Field(min_length=1)
    namespace: str = Field(default="prod", min_length=1)


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intake: bool = False
    paths: list[str] = Field(min_length=1)
    done_when: str
    deploy: DeployTarget | None = None
    workers_per_run: int = Field(default=3, ge=1, le=3)

    @field_validator("done_when")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("done_when must not be empty")
        return value


def parse_project_config(text: str) -> ProjectConfig:
    """Parse the YAML; any problem is a ``ValueError`` with the reason."""
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"project.yaml is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("project.yaml must be a mapping")
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"project.yaml is invalid: {exc.errors()[0]['msg']}") from exc
