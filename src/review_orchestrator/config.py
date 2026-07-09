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
    github_app_id: str | None = None
    github_private_key_path: str | None = None
    github_api_base_url: str = "https://api.github.com"
    openhands_base_url: str = "http://localhost:3000"
    openhands_api_token: str | None = None
    openhands_review_skill: str = "code-review"
    openhands_review_profile: str = "default"
    review_bot_login: str = "review-agent"
    workspace_root: str = "./.workspaces"
    git_cache_root: str = "./.git-cache"
    review_run_timeout_seconds: int = 1800
    review_run_soft_timeout_seconds: int = 900
    retry_max_attempts: int = 2
    retry_initial_delay_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
