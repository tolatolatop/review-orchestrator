from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REVIEW_",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "Review Orchestrator"
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
    workspace_root: str = Field(
        default="./data/workspaces",
        description="Root directory for prepared PR workspaces.",
    )
    git_cache_root: str = Field(
        default="./data/git-cache",
        description="Root directory for bare Git mirror caches.",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
