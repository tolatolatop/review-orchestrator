"""Database engine, sessions, and startup migrations."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import MetaData, Table, func, inspect, select, text
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


async def init_models(
    engine: AsyncEngine,
    settings: Settings | None = None,
) -> None:
    # Import registers SQLAlchemy models with metadata.
    import review_orchestrator.domain.models  # noqa: F401

    settings = settings or get_settings()
    async with engine.begin() as conn:
        preset_table_missing = await conn.run_sync(
            lambda connection: "agent_preset"
            not in inspect(connection).get_table_names()
        )
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_unified_tasks)
        await conn.run_sync(_migrate_agent_runtime_columns)
        if preset_table_missing:
            await conn.run_sync(
                lambda connection: _seed_agent_presets(connection, settings)
            )


def _seed_agent_presets(connection: Connection, settings: Settings) -> None:
    """Seed default resources once when the Agent Preset table is introduced."""

    from review_orchestrator.domain.models import AgentPreset, ReviewConfig

    if connection.execute(select(func.count(AgentPreset.id))).scalar_one() > 0:
        return
    now = datetime.now(UTC)
    rows: list[dict[str, object]] = [
        {
            "name": "default-review",
            "description": "Global default preset for pull request reviews.",
            "task_kind": "review",
            "scope": "global",
            "scope_key": "global",
            "provider": None,
            "repo_full_name": None,
            "agent_id": settings.pi_agent_review_agent,
            "task_type": "code-review",
            "repository_skills_json": [settings.pi_agent_review_skill],
            "model_json": None,
            "tools_json": None,
            "limits_json": None,
            "enabled": True,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        },
        {
            "name": "default-agent-task",
            "description": "Global default preset for pull request AgentTasks.",
            "task_kind": "agent_task",
            "scope": "global",
            "scope_key": "global",
            "provider": None,
            "repo_full_name": None,
            "agent_id": settings.agent_command_agent,
            "task_type": "message-command",
            "repository_skills_json": [settings.agent_command_skill],
            "model_json": None,
            "tools_json": None,
            "limits_json": None,
            "enabled": True,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        },
    ]
    inspector = inspect(connection)
    review_config_columns = {
        column["name"] for column in inspector.get_columns("review_config")
    }
    required_review_config_columns = {
        "provider",
        "repo_full_name",
        "default_review_skill",
        "default_agent_command_skill",
    }
    if required_review_config_columns <= review_config_columns:
        configs = connection.execute(
            select(
                ReviewConfig.provider,
                ReviewConfig.repo_full_name,
                ReviewConfig.default_review_skill,
                ReviewConfig.default_agent_command_skill,
            )
        ).mappings()
        for config in configs:
            digest = sha256(
                f"{config['provider']}:{config['repo_full_name']}".encode()
            ).hexdigest()[:16]
            common = {
                "scope": "repository",
                "scope_key": (
                    f"repository:{config['provider']}:{config['repo_full_name']}"
                ),
                "provider": config["provider"],
                "repo_full_name": config["repo_full_name"],
                "model_json": None,
                "tools_json": None,
                "limits_json": None,
                "enabled": True,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            rows.extend(
                [
                    {
                        **common,
                        "name": f"migrated-review-{digest}",
                        "description": "Migrated repository review selection.",
                        "task_kind": "review",
                        "agent_id": settings.pi_agent_review_agent,
                        "task_type": "code-review",
                        "repository_skills_json": [
                            config["default_review_skill"]
                        ],
                    },
                    {
                        **common,
                        "name": f"migrated-agent-task-{digest}",
                        "description": "Migrated repository AgentTask selection.",
                        "task_kind": "agent_task",
                        "agent_id": settings.agent_command_agent,
                        "task_type": "message-command",
                        "repository_skills_json": [
                            config["default_agent_command_skill"]
                        ],
                    },
                ]
            )
    connection.execute(AgentPreset.__table__.insert(), rows)


_LEGACY_TASK_CONTROL_COLUMNS = (
    "status",
    "stage",
    "lock_owner",
    "locked_until",
    "started_at",
    "completed_at",
    "deadline_at",
    "created_at",
    "updated_at",
)


def _migrate_unified_tasks(connection: Connection) -> None:
    """Move legacy review/agent lifecycle rows into the unified task table.

    Joined-table inheritance requires control columns to be owned by ``task``.
    Existing feature databases may still have those columns on the domain
    tables, so copy their values and remove only the migrated columns. The
    domain tables and their foreign keys remain intact.
    """

    from review_orchestrator.domain.models import Task

    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    if "task" not in table_names:
        return

    existing_task_ids = set(connection.execute(select(Task.__table__.c.id)).scalars())
    now = datetime.now(UTC)
    specs = (
        ("review_run", "review", "code-review"),
        ("agent_task", "agent", "pr-assistant"),
    )
    for table_name, kind, capability_id in specs:
        if table_name not in table_names:
            continue
        legacy = Table(table_name, MetaData(), autoload_with=connection)
        rows = list(connection.execute(select(legacy)).mappings())
        inserts: list[dict[str, object]] = []
        for row in rows:
            task_id = str(row["id"])
            if task_id in existing_task_ids:
                continue
            created_at = row.get("created_at") or now
            is_interactive = (
                kind == "agent" and row.get("task_type") == "message_command"
            )
            is_manual = kind == "review" and row.get("trigger_type") == "manual"
            queue = (
                "interactive"
                if is_interactive
                else "manual-review"
                if is_manual
                else "webhook-review"
            )
            priority = 80 if is_interactive else 60 if is_manual else 40
            repository = row.get("repo_full_name")
            provider = row.get("provider")
            pr_number = row.get("pull_request_number")
            head_sha = row.get("head_sha")
            concurrency_parts = [provider, repository, "pr", pr_number]
            if kind == "review" and head_sha:
                concurrency_parts.extend(["head", head_sha])
            concurrency_key = ":".join(
                str(part) for part in concurrency_parts if part is not None
            ) or None
            status = str(row.get("status") or "queued")
            inserts.append(
                {
                    "id": task_id,
                    "kind": kind,
                    "capability_id": capability_id,
                    "status": status,
                    "stage": row.get("stage"),
                    "execution_status": _legacy_execution_status(status),
                    "delivery_status": "not_required",
                    "queue": queue,
                    "priority": priority,
                    "effective_priority": priority,
                    "available_at": created_at,
                    "concurrency_key": concurrency_key,
                    "resource_class": "agent-standard",
                    "max_attempts": 2,
                    "lock_owner": row.get("lock_owner"),
                    "locked_until": row.get("locked_until"),
                    "started_at": row.get("started_at"),
                    "completed_at": row.get("completed_at"),
                    "deadline_at": row.get("deadline_at"),
                    "created_at": created_at,
                    "updated_at": row.get("updated_at") or created_at,
                }
            )
            existing_task_ids.add(task_id)
        if inserts:
            connection.execute(Task.__table__.insert(), inserts)
        _drop_legacy_task_control_columns(connection, table_name)


def _legacy_execution_status(status: str) -> str:
    if status == "completed":
        return "completed"
    if status in {"failed", "cancelled", "superseded"}:
        return "failed"
    if status == "running":
        return "running"
    return "pending"


def _drop_legacy_task_control_columns(
    connection: Connection,
    table_name: str,
) -> None:
    inspector = inspect(connection)
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    moved = [name for name in _LEGACY_TASK_CONTROL_COLUMNS if name in columns]
    if not moved:
        return

    quote = connection.dialect.identifier_preparer.quote
    for index in inspector.get_indexes(table_name):
        indexed_columns = set(index.get("column_names") or [])
        if indexed_columns.intersection(moved):
            connection.exec_driver_sql(
                f"DROP INDEX IF EXISTS {quote(str(index['name']))}"
            )
    for name in moved:
        connection.exec_driver_sql(
            f"ALTER TABLE {quote(table_name)} DROP COLUMN {quote(name)}"
        )


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
