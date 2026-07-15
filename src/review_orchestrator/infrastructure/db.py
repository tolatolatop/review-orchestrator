"""Database engine, sessions, and additive startup migrations."""

from collections.abc import AsyncGenerator

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from starlette.requests import Request

from review_orchestrator.infrastructure.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("sqlite:///"):
        return database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return database_url


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    return create_async_engine(
        normalize_database_url(settings.database_url),
        future=True,
        pool_pre_ping=True,
    )


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_models(engine: AsyncEngine) -> None:
    # Import registers SQLAlchemy models with metadata.
    import review_orchestrator.domain.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_agent_runtime_columns)


def _migrate_agent_runtime_columns(connection: Connection) -> None:
    """Add generic pi-agent columns and carry forward legacy session ids.

    The project does not yet use Alembic, so startup performs the small additive
    migration required by the execution-backend cutover. The statements are
    compatible with both SQLite and PostgreSQL and are idempotent.
    """

    review_run_columns = {
        column["name"] for column in inspect(connection).get_columns("review_run")
    }
    additions = {
        "agent_session_id": "VARCHAR(128)",
        "agent_status": "VARCHAR(32)",
        "agent_provider": "VARCHAR(64)",
        "agent_model": "VARCHAR(128)",
        "agent_thinking_level": "VARCHAR(16)",
    }
    for name, sql_type in additions.items():
        if name not in review_run_columns:
            connection.execute(
                text(f"ALTER TABLE review_run ADD COLUMN {name} {sql_type}")
            )
    if "openhands_conversation_id" in review_run_columns:
        connection.execute(
            text(
                "UPDATE review_run "
                "SET agent_session_id = openhands_conversation_id "
                "WHERE agent_session_id IS NULL "
                "AND openhands_conversation_id IS NOT NULL"
            )
        )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_review_run_agent_session_id "
            "ON review_run (agent_session_id)"
        )
    )

    review_session_columns = {
        column["name"] for column in inspect(connection).get_columns("review_session")
    }
    if "agent_session_id" not in review_session_columns:
        connection.execute(
            text("ALTER TABLE review_session ADD COLUMN agent_session_id VARCHAR(128)")
        )
    if "openhands_conversation_id" in review_session_columns:
        connection.execute(
            text(
                "UPDATE review_session "
                "SET agent_session_id = openhands_conversation_id "
                "WHERE agent_session_id IS NULL "
                "AND openhands_conversation_id IS NOT NULL"
            )
        )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_review_session_agent_session_id "
            "ON review_session (agent_session_id)"
        )
    )

    agent_task_columns = {
        column["name"] for column in inspect(connection).get_columns("agent_task")
    }
    task_additions = {
        "stage": "VARCHAR(64)",
        "source_kind": "VARCHAR(64)",
        "source_comment_id": "VARCHAR(128)",
        "source_url": "TEXT",
        "source_author_login": "VARCHAR(255)",
        "command_text": "TEXT",
        "head_sha": "VARCHAR(80)",
        "workspace_path": "TEXT",
        "response_comment_id": "VARCHAR(128)",
        "response_body_hash": "VARCHAR(128)",
        "response_published_at": "TIMESTAMP",
        "publish_attempts": "INTEGER DEFAULT 0",
        "last_publish_error": "TEXT",
        "agent_session_id": "VARCHAR(128)",
        "agent_status": "VARCHAR(32)",
        "agent_provider": "VARCHAR(64)",
        "agent_model": "VARCHAR(128)",
        "agent_thinking_level": "VARCHAR(16)",
        "result_text": "TEXT",
        "failure_code": "VARCHAR(64)",
        "attempt": "INTEGER DEFAULT 1",
        "agent_start_attempts": "INTEGER DEFAULT 0",
        "lock_owner": "VARCHAR(128)",
        "locked_until": "TIMESTAMP",
        "started_at": "TIMESTAMP",
        "completed_at": "TIMESTAMP",
        "deadline_at": "TIMESTAMP",
        "soft_timeout_emitted_at": "TIMESTAMP",
        "hard_timeout_emitted_at": "TIMESTAMP",
    }
    for name, sql_type in task_additions.items():
        if name not in agent_task_columns:
            connection.execute(
                text(f"ALTER TABLE agent_task ADD COLUMN {name} {sql_type}")
            )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_task_provider_event "
            "ON agent_task (provider_event_id) "
            "WHERE provider_event_id IS NOT NULL"
        )
    )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_agent_task_agent_session_id "
            "ON agent_task (agent_session_id)"
        )
    )

    review_config_columns = {
        column["name"] for column in inspect(connection).get_columns("review_config")
    }
    config_additions = {
        "agent_commands_enabled": "BOOLEAN DEFAULT TRUE",
        "default_agent_command_skill": "VARCHAR(128) DEFAULT 'pr-assistant'",
        "default_agent_command_profile": "VARCHAR(128) DEFAULT 'default'",
    }
    for name, sql_type in config_additions.items():
        if name not in review_config_columns:
            connection.execute(
                text(f"ALTER TABLE review_config ADD COLUMN {name} {sql_type}")
            )


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session
