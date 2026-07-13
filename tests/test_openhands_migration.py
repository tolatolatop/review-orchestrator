from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, select

SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "deploy"
    / "openhands"
    / "migrate_sqlite_to_postgres.py"
)
SPEC = spec_from_file_location("migrate_sqlite_to_postgres", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
migration = module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def _engine(path: Path, rows: list[dict]):
    engine = create_engine(f"sqlite:///{path}")
    metadata = MetaData()
    table = Table(
        "conversation_metadata",
        metadata,
        Column("conversation_id", String, primary_key=True),
        Column("metadata", JSON, nullable=False),
    )
    metadata.create_all(engine)
    if rows:
        with engine.begin() as connection:
            connection.execute(table.insert(), rows)
    return engine, table


def test_migration_merges_missing_rows_and_preserves_target_only_rows(
    tmp_path: Path,
) -> None:
    source, _ = _engine(
        tmp_path / "source.db",
        [
            {"conversation_id": "shared", "metadata": {"state": "same"}},
            {"conversation_id": "source-only", "metadata": {"state": "source"}},
        ],
    )
    target, target_table = _engine(
        tmp_path / "target.db",
        [
            {"conversation_id": "shared", "metadata": {"state": "same"}},
            {"conversation_id": "target-only", "metadata": {"state": "target"}},
        ],
    )
    migration.TABLE_NAMES = ("conversation_metadata",)
    try:
        assert migration.migrate_tables(source, target) == {
            "conversation_metadata": 1
        }
        with target.connect() as connection:
            rows = {
                row.conversation_id: row.metadata
                for row in connection.execute(select(target_table))
            }
        assert rows == {
            "shared": {"state": "same"},
            "source-only": {"state": "source"},
            "target-only": {"state": "target"},
        }
        assert migration.migrate_tables(source, target) == {
            "conversation_metadata": 0
        }
    finally:
        source.dispose()
        target.dispose()


def test_migration_fails_closed_on_conflicting_primary_key(tmp_path: Path) -> None:
    source, _ = _engine(
        tmp_path / "source.db",
        [{"conversation_id": "shared", "metadata": {"state": "source"}}],
    )
    target, target_table = _engine(
        tmp_path / "target.db",
        [{"conversation_id": "shared", "metadata": {"state": "target"}}],
    )
    migration.TABLE_NAMES = ("conversation_metadata",)
    try:
        with pytest.raises(RuntimeError, match="Refusing conflicting migration"):
            migration.migrate_tables(source, target)
        with target.connect() as connection:
            rows = list(connection.execute(select(target_table)))
        assert len(rows) == 1
        assert rows[0].metadata == {"state": "target"}
    finally:
        source.dispose()
        target.dispose()


def test_sqlite_backups_are_content_addressed_and_never_overwritten(
    tmp_path: Path,
) -> None:
    source = tmp_path / "openhands.db"
    with migration.sqlite3.connect(source) as database:
        database.execute("CREATE TABLE example (id INTEGER PRIMARY KEY)")
        database.execute("INSERT INTO example VALUES (1)")

    first = migration.backup_sqlite(source)
    assert first.is_file()
    assert migration.backup_sqlite(source) == first

    with migration.sqlite3.connect(source) as database:
        database.execute("INSERT INTO example VALUES (2)")
    second = migration.backup_sqlite(source)

    assert second.is_file()
    assert second != first
    assert first.is_file()
