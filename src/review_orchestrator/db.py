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

from review_orchestrator.config import Settings, get_settings


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
    import review_orchestrator.models  # noqa: F401

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
        column["name"]
        for column in inspect(connection).get_columns("review_session")
    }
    if "agent_session_id" not in review_session_columns:
        connection.execute(
            text(
                "ALTER TABLE review_session "
                "ADD COLUMN agent_session_id VARCHAR(128)"
            )
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


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session
