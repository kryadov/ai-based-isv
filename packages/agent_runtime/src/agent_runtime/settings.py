"""Runtime constants and worker settings for the agent runtime."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

OPENCODE_IMAGE: str = "ghcr.io/anomalyco/opencode:2.2.0"
OPENCODE_PORT: int = 4096


class WorkerSettings(BaseSettings):
    """Run-scoped worker configuration, loaded from ``AGENT_RUNTIME_*`` env vars."""

    model_config = SettingsConfigDict(env_prefix="AGENT_RUNTIME_")

    api_url: str
    run_id: int
    agent: str = "worker"
    poll_interval_seconds: float = 2.0
    lease_ttl_seconds: int = 300
    workspace_dir: Path = Path("./.agent-runtime-workspace")
    repo_url: str
    base_branch: str = "main"
    opencode_base_url: str | None = None
    opencode_port: int = 4096
    litellm_base_url: str
    litellm_api_key: str = ""
    model: str
    git_token: str = ""
