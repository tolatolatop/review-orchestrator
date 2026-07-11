"""Initialize OpenHands PostgreSQL and migrate a legacy SQLite database safely."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    DateTime,
    MetaData,
    Uuid,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql.schema import Table

SQLITE_PATH = Path(os.getenv("OPENHANDS_SQLITE_PATH", "/.openhands/openhands.db"))
BACKUP_PATH = SQLITE_PATH.with_suffix(".db.pre-postgres.bak")
ALEMBIC_INI = Path("/app/openhands/app_server/app_lifespan/alembic.ini")
TABLE_NAMES = (
    "app_conversation_start_task",
    "event_callback",
    "event_callback_result",
    "v1_remote_sandbox",
    "conversation_metadata",
    "pending_messages",
)


def postgres_url() -> str:
    host = os.environ["DB_HOST"]
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "openhands")
    user = os.getenv("DB_USER", "postgres")
    password = os.environ["DB_PASS"]
    from urllib.parse import quote_plus

    return (
        f"postgresql+pg8000://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{quote_plus(name)}"
    )


def prepare_openhands_postgres(engine: Engine) -> None:
    """Work around the OpenHands 1.8 migration 002 enum creation bug."""
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                DO $$ BEGIN
                    CREATE TYPE eventcallbackstatus AS ENUM (
                        'ACTIVE', 'DISABLED', 'COMPLETED', 'ERROR'
                    );
                EXCEPTION
                    WHEN duplicate_object THEN NULL;
                END $$
                """
            )
        )


def _alembic_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        table_exists = connection.scalar(text("SELECT to_regclass('alembic_version')"))
        if table_exists is None:
            return None
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


def upgrade_postgres(engine: Engine) -> None:
    if not ALEMBIC_INI.is_file():
        raise RuntimeError(f"OpenHands Alembic config is missing: {ALEMBIC_INI}")
    config = Config(str(ALEMBIC_INI))
    revision = _alembic_revision(engine)
    if revision is None or revision in {f"{number:03d}" for number in range(1, 10)}:
        command.upgrade(config, "009")
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            connection.execute(
                text(
                    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                    "ix_event_callback_conversation_id_status_event_kind "
                    "ON event_callback (conversation_id, status, event_kind)"
                )
            )
        command.stamp(config, "010")
    command.upgrade(config, "head")


def backup_sqlite(source: Path = SQLITE_PATH, backup: Path = BACKUP_PATH) -> None:
    if backup.exists():
        return
    with sqlite3.connect(source) as source_db, sqlite3.connect(backup) as backup_db:
        source_db.backup(backup_db)


def _primary_key(table: Table) -> tuple[str, ...]:
    keys = tuple(column.name for column in table.primary_key.columns)
    if not keys:
        raise RuntimeError(f"Cannot migrate table without a primary key: {table.name}")
    return keys


def _normalize(value: Any, column: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, Uuid) and not isinstance(value, uuid.UUID):
        return uuid.UUID(str(value))
    if isinstance(column.type, DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(column.type, JSON) and isinstance(value, str):
        return json.loads(value)
    return value


def _source_rows(
    connection: Connection, source_name: str, target: Table
) -> list[dict]:
    target_columns = {column.name: column for column in target.columns}
    rows = []
    # Use driver SQL so SQLite's UUID columns are not mis-reflected as Numeric.
    for row in connection.exec_driver_sql(
        f'SELECT * FROM "{source_name}"'
    ).mappings():
        rows.append(
            {
                name: _normalize(value, target_columns[name])
                for name, value in row.items()
                if name in target_columns
            }
        )
    return rows


def migrate_tables(source_engine: Engine, target_engine: Engine) -> dict[str, int]:
    source_metadata = MetaData()
    source_metadata.reflect(source_engine, only=lambda name, _: name in TABLE_NAMES)
    target_metadata = MetaData()
    target_metadata.reflect(target_engine, only=lambda name, _: name in TABLE_NAMES)

    missing = set(source_metadata.tables) - set(target_metadata.tables)
    if missing:
        raise RuntimeError(f"PostgreSQL schema is missing tables: {sorted(missing)}")

    copied: dict[str, int] = {}
    with source_engine.connect() as source, target_engine.begin() as target:
        for name in TABLE_NAMES:
            source_table = source_metadata.tables.get(name)
            if source_table is None:
                continue
            target_table = target_metadata.tables[name]
            rows = _source_rows(source, name, target_table)
            source_keys = {
                tuple(row[key] for key in _primary_key(target_table)) for row in rows
            }
            target_rows = target.execute(select(target_table)).mappings()
            target_keys = {
                tuple(row[key] for key in _primary_key(target_table))
                for row in target_rows
            }
            if source_keys <= target_keys:
                copied[name] = 0
                continue
            if target_keys:
                raise RuntimeError(
                    f"Refusing partial migration for {name}: PostgreSQL already "
                    "contains different rows"
                )
            if rows:
                target.execute(target_table.insert(), rows)
            actual = target.scalar(select(func.count()).select_from(target_table))
            if actual != len(rows):
                raise RuntimeError(
                    f"Row validation failed for {name}: expected {len(rows)}, "
                    f"got {actual}"
                )
            copied[name] = len(rows)
    return copied


def main() -> int:
    target_engine = create_engine(postgres_url())
    try:
        prepare_openhands_postgres(target_engine)
        upgrade_postgres(target_engine)
        if not SQLITE_PATH.is_file():
            print(
                "PostgreSQL schema is ready; no legacy OpenHands SQLite "
                "database found."
            )
            return 0

        backup_sqlite()
        source_engine = create_engine(f"sqlite:///{SQLITE_PATH}")
        try:
            copied = migrate_tables(source_engine, target_engine)
        finally:
            source_engine.dispose()
    finally:
        target_engine.dispose()
    print(
        "OpenHands SQLite migration verified: "
        + ", ".join(f"{name}={count}" for name, count in copied.items())
    )
    print(f"Legacy backup retained at {BACKUP_PATH}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"OpenHands database migration failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        raise
