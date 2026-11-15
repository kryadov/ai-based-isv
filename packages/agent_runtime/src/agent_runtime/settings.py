"""Runtime constants and worker settings for the agent runtime."""

from pathlib import Path
from typing import Literal

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
    session_timeout_seconds: float = 900.0  # per attempt; a stuck harness fails the attempt
    workspace_dir: Path = Path("./.agent-runtime-workspace")
    repo_url: str = ""
    base_branch: str = "main"
    opencode_base_url: str | None = None
    opencode_port: int = 4096
    litellm_base_url: str = ""
    litellm_api_key: str = ""
    model: str = ""
    git_token: str = ""
    # --- k3s sandbox backend (M4b) ---
    # v0 contract: ONE worker per run — the age-based orphan sweep is unsafe
    # with concurrent workers.
    sandbox_backend: Literal["docker", "k3s"] = "docker"
    k8s_namespace: str = "agents"
    nodeport_range_low: int = 30100
    nodeport_range_high: int = 30110
    host_ip: str = ""  # LiteLLM hostAliases target; required for the docker path only
    vault_root_token_path: Path = Path("infra/.vault-root-token")
    openbao_pod_label: str = "app.kubernetes.io/name=openbao"
    openbao_namespace: str = "platform"
    vault_root_token: str = ""  # explicit override; otherwise read from vault_root_token_path
    # --- MCP servers registered into the sandbox harness (M4c; empty = omit) ---
    mcp_task_source_url: str = ""
    mcp_llm_url: str = ""
    mcp_observability_url: str = ""  # M6: read-only rollout/metrics view
    # --- tracker status loop (M4c): task_ref "owner/repo#N"; empty = disabled ---
    task_ref: str = ""
    task_source_url: str = ""  # task-source MCP endpoint reachable from the worker host
    # --- gate corridor (M5): PR → CI → review → fix rounds → HITL → merge ---
    gates_enabled: bool = False  # False keeps the M4 behaviour (complete on push)
    max_fix_rounds: int = 2
    ci_timeout_s: float = 1200.0
    hitl_poll_s: float = 15.0
    reviewer_model: str = "reviewer"  # LiteLLM alias (cross-family vs the implementer)
    # Host-reachable LiteLLM for the reviewer (the worker runs on the host; the
    # sandbox's litellm_base_url may be in-cluster). Empty = litellm_base_url.
    reviewer_base_url: str = ""
    gitea_url: str = ""  # Gitea API base when it differs from the repo_url host
    platform_repo: bool = False  # force "platform repo" semantics: never auto-merge (§13.7)
    # Repos ("owner/repo") that ARE this platform (self-dev level 2): always HITL.
    platform_repos: list[str] = ["swarm-lab/ai-based-isv"]
    # --- deploy (M6): merged main → Argo Rollouts canary in prod ---
    deploy_enabled: bool = False
    deploy_app: str = "isv-product"
    deploy_namespace: str = "prod"
    prometheus_url: str = "http://localhost:30090"  # host-reachable (NodePort)
    deploy_timeout_s: float = 900.0

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
