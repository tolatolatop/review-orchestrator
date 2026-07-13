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
    openhands_base_url: str | None = Field(
        default="http://localhost:3000",
        description="Base URL for the OpenHands App Server API.",
    )
    openhands_ui_base_url: str | None = Field(
        default=None,
        description=(
            "Operator-facing OpenHands UI base URL used to build safe "
            "conversation links. If unset, observability responses make that "
            "disabled state explicit."
        ),
    )
    openhands_api_token: str | None = None
    openhands_review_skill: str = "code-review"
    openhands_review_profile: str = "default"
    openhands_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description="HTTP timeout for OpenHands App Server requests.",
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
    workspace_root: str = "./.workspaces"
    git_cache_root: str = "./.git-cache"
    review_run_timeout_seconds: int = 1800
    review_run_soft_timeout_seconds: int = 900
    worker_poll_interval_seconds: float = Field(default=5.0, gt=0)
    worker_lock_seconds: int = Field(default=300, gt=0)
    retry_max_attempts: int = 2
    retry_initial_delay_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
