from pathlib import Path

from sqlalchemy import inspect, text

from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, init_models


async def test_init_models_migrates_legacy_session_identifiers(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/legacy.db",
        github_app_id=None,
        github_private_key_path=None,
    )
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE review_run ("
                    "id VARCHAR(36) PRIMARY KEY, "
                    "openhands_conversation_id VARCHAR(80))"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE review_session ("
                    "id VARCHAR(36) PRIMARY KEY, "
                    "openhands_conversation_id VARCHAR(128))"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO review_run "
                    "(id, openhands_conversation_id) "
                    "VALUES ('run-1', 'legacy-session-1')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO review_session "
                    "(id, openhands_conversation_id) "
                    "VALUES ('review-session-1', 'legacy-session-1')"
                )
            )

        await init_models(engine)

        async with engine.connect() as connection:
            review_run_columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("review_run")
                }
            )
            migrated_run = (
                await connection.execute(
                    text(
                        "SELECT agent_session_id FROM review_run "
                        "WHERE id = 'run-1'"
                    )
                )
            ).scalar_one()
            migrated_session = (
                await connection.execute(
                    text(
                        "SELECT agent_session_id FROM review_session "
                        "WHERE id = 'review-session-1'"
                    )
                )
            ).scalar_one()

        assert {
            "agent_session_id",
            "agent_status",
            "agent_provider",
            "agent_model",
            "agent_thinking_level",
        } <= review_run_columns
        assert migrated_run == "legacy-session-1"
        assert migrated_session == "legacy-session-1"
    finally:
        await engine.dispose()


async def test_init_models_adds_message_command_task_columns(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/legacy-task.db",
        github_app_id=None,
        github_private_key_path=None,
    )
    engine = create_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE agent_task ("
                    "id VARCHAR(36) PRIMARY KEY, "
                    "provider_event_id VARCHAR(36))"
                )
            )
            await connection.execute(
                text("CREATE TABLE review_config (id VARCHAR(36) PRIMARY KEY)")
            )

        await init_models(engine)

        async with engine.connect() as connection:
            task_columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("agent_task")
                }
            )
            config_columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("review_config")
                }
            )

        assert {
            "stage",
            "command_text",
            "response_comment_id",
            "agent_session_id",
            "result_text",
            "failure_code",
            "soft_timeout_emitted_at",
            "hard_timeout_emitted_at",
        } <= task_columns
        assert {
            "agent_commands_enabled",
            "default_agent_command_skill",
            "default_agent_command_profile",
        } <= config_columns
    finally:
        await engine.dispose()
