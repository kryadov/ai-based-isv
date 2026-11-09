"""Настройки agent_api (env AGENT_API_DATABASE_URL)."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./agent-api.db"

    model_config = {"env_prefix": "AGENT_API_"}


settings = Settings()
