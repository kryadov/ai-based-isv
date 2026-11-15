"""agent_api settings (env AGENT_API_DATABASE_URL)."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./agent-api.db"
    # --- organization panel (M10): humans only; empty users = panel disabled ---
    ui_users: str = ""  # "name:sha256hex(password):role,..."
    session_secret: str = ""
    projects_file: str = "infra/projects.yaml"
    automations_dir: str = "infra/automations"

    model_config = {"env_prefix": "AGENT_API_"}


settings = Settings()
