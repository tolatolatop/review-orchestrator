"""Runtime configuration."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "Review Orchestrator"
    app_env: str = "local"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000
    database_url: str = Field(
        default="sqlite+aiosqlite:///./review_orchestrator.db",
        description="SQLAlchemy async database URL for SQLite or PostgreSQL.",
    )
    github_webhook_secret: str | None = Field(
        default=None,
        description=(
            "GitHub App webhook secret. If unset, signature verification is "
            "skipped for local development."
        ),
    )
    pi_agent_base_url: str | None = Field(
        default="http://localhost:3210",
        description="Base URL for the isolated pi-agent runtime API.",
    )
    pi_agent_runtime_token: str | None = Field(
        default=None,
        description="Bearer token used to authenticate to the pi-agent runtime.",
    )
    pi_agent_review_skill: str = "code-review"
    pi_agent_review_agent: str = "code-review"
    pi_agent_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="HTTP timeout for pi-agent runtime requests.",
    )
    github_app_id: str | None = None
    github_private_key_path: str | None = None
    github_installation_id: int | None = Field(default=None, gt=0)
    github_installation_token: str | None = Field(
        default=None,
        description=(
            "GitHub installation or fine-grained token used by the worker for "
            "PR file lookup and comment publishing."
        ),
    )
    github_api_base_url: str = "https://api.github.com"
    review_bot_login: str = "review-agent"
    agent_command_enabled: bool = True
    agent_command_skill: str = "pr-assistant"
    agent_command_agent: str = "pr-assistant"
    agent_task_soft_timeout_seconds: int = Field(default=120, gt=0)
    agent_task_timeout_seconds: int = Field(default=600, gt=0)
    agent_task_max_history_turns: int = Field(default=6, ge=0, le=20)
    agent_task_max_history_chars: int = Field(default=24000, ge=0)
    agent_task_allowed_associations: str = "OWNER,MEMBER,COLLABORATOR"
    agent_task_max_command_chars: int = Field(default=8000, gt=0, le=30000)
    gitlab_webhook_secret: str | None = Field(
        default=None,
        description=(
            "GitLab webhook shared token. If unset, token verification is skipped."
        ),
    )
    gitlab_api_base_url: str = "https://gitlab.com/api/v4"
    gitlab_api_token: str | None = Field(
        default=None,
        description="GitLab API token used by the worker for MR lookup and notes.",
    )
    platform_diagnostics_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="HTTP timeout for read-only provider permission diagnostics.",
    )
    provider_api_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="HTTP timeout for GitHub and GitLab API requests.",
    )
    workspace_root: str = "./.workspaces"
    git_cache_root: str = "./.git-cache"
    review_run_timeout_seconds: int = 1800
    review_run_soft_timeout_seconds: int = 900
    worker_poll_interval_seconds: float = Field(default=5.0, gt=0)
    worker_lock_seconds: int = Field(default=300, gt=0)
    task_priority_aging_seconds: int = Field(default=300, gt=0)
    task_scheduler_scan_limit: int = Field(default=64, gt=0, le=1000)
    task_resource_capacities: str = (
        "user=4,repository=4,pr=1,pr_head=1,comment=1,model=4,concurrency=1"
    )
    retry_max_attempts: int = 2
    retry_initial_delay_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
