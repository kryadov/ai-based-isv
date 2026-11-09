"""Runtime constants and worker settings for the agent runtime."""

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

OPENCODE_IMAGE: str = "ghcr.io/anomalyco/opencode:2.2.0"
OPENCODE_PORT: int = 4096


class WorkerSettings(BaseSettings):
    """Run-scoped worker configuration, loaded from ``AGENT_RUNTIME_*`` env vars."""

    model_config = SettingsConfigDict(env_prefix="AGENT_RUNTIME_")

    api_url: str = ""
    run_id: int = 0
    agent: str = "worker"
    poll_interval_seconds: float = 2.0
    lease_ttl_seconds: int = 300
    workspace_dir: Path = Path("./.agent-runtime-workspace")
    repo_url: str = ""
    base_branch: str = "main"
    opencode_base_url: str | None = None
    opencode_port: int = 4096
    litellm_base_url: str = ""
    litellm_api_key: str = ""
    model: str = ""
    git_token: str = ""

    @model_validator(mode="after")
    def _required_at_runtime(self) -> "WorkerSettings":
        """Required values that cannot be left at their empty defaults at run time."""
        missing: list[str] = []
        if not self.api_url:
            missing.append("api_url")
        if self.run_id <= 0:
            missing.append("run_id")
        if not self.repo_url:
            missing.append("repo_url")
        if not self.litellm_base_url:
            missing.append("litellm_base_url")
        if not self.model:
            missing.append("model")
        if missing:
            raise ValueError(f"missing required worker settings: {', '.join(missing)}")
        return self
