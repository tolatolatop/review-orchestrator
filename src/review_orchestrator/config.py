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


@lru_cache
def get_settings() -> Settings:
    return Settings()
