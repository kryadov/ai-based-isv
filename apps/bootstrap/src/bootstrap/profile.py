"""Organization profile: owner choices, saved to infra/env.local.yaml (gitignored).

The profile contains no secrets — only questionnaire decisions.
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
    """Questionnaire decisions (no secrets)."""

    target: str = Field(default="laptop-k3d", description="deployment target")
    tracker: str = Field(
        default=TRACKER_DEPLOY,
        description="tracker: deploy-openproject | connect-existing",
    )
    tracker_existing_url: str = Field(
        default="", description="URL of the existing tracker (if connect-existing)"
    )
    llm: str = Field(default=LLM_HOST_OLLAMA, description="models: host-ollama | external-api")
    demo: bool = Field(default=True, description="seed the isv-product demo repo")


def save(profile: Profile, path: Path) -> Path:
    """Save the profile (create the parent directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(profile.model_dump(), sort_keys=False, allow_unicode=True))
    return path


def load(path: Path) -> Profile:
    """Load the profile from a file."""
    data = yaml.safe_load(path.read_text()) or {}
    return Profile(**data)
