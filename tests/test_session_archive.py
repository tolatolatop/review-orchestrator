import subprocess
from pathlib import Path

import pytest
from sqlalchemy import func, select

from review_orchestrator.application.services import (
    get_session_archive,
    list_task_session_archives,
)
from review_orchestrator.application.session_archive import (
    _capture_workspace_diff,
    archive_agent_session,
)
from review_orchestrator.domain.models import ReviewRun, SessionArchive, TaskAttempt
from review_orchestrator.infrastructure.config import Settings
from review_orchestrator.infrastructure.db import (
    create_engine,
    create_session_factory,
    init_models,
)
from review_orchestrator.integrations.pi_agent import PiAgentSession


@pytest.fixture
async def session_factory(tmp_path: Path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db")
    engine = create_engine(settings)
    await init_models(engine)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=True,
        capture_output=True,
    )


async def test_archive_upserts_redacted_session_attempt_and_workspace_diff(
    session_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    source = workspace / "main.py"
    source.write_text("answer = 1\n")
    _git(workspace, "add", "main.py")
    _git(workspace, "commit", "-m", "initial")
    source.write_text("answer = 2\nsecret = 'sk-abcdefghijklmnopqrstuv'\n")
    monkeypatch.setattr(
        "review_orchestrator.application.session_archive.MAX_WORKSPACE_DIFF_BYTES",
        120,
    )

    async with session_factory() as session:
        task = ReviewRun(
            provider="github",
            repo_full_name="owner/repo",
            pull_request_number=42,
            head_sha="b" * 40,
            workspace_path=str(workspace),
            resolved_preset_json={"tools": ["read_file", "bash"]},
        )
        session.add(task)
        await session.commit()

        runtime = PiAgentSession(
            id="run-1",
            status="running",
            stage="tool:bash",
            provider="openai",
            model="gpt-5.4",
            skills=["review"],
            tools=["read_file", "bash"],
            session_archive={
                "entries": [
                    {
                        "role": "tool",
                        "authorization": "Bearer abcdefghijklmnop",
                        "content": "token sk-abcdefghijklmnopqrstuv",
                    }
                ],
                "stats": {"tokens": 42},
            },
        )
        first = await archive_agent_session(session, task, runtime)
        await session.commit()

        completed = runtime.model_copy(
            update={"status": "completed", "stage": "completed"}
        )
        second = await archive_agent_session(session, task, completed)
        await session.commit()

        archive_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(SessionArchive)
                )
            ).scalar_one()
        )
        attempt_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(TaskAttempt)
                )
            ).scalar_one()
        )
        listed = await list_task_session_archives(session, task.id)
        fetched = await get_session_archive(session, first.id)

    assert first.id == second.id
    assert archive_count == 1
    assert attempt_count == 1
    assert listed is not None
    assert len(listed.items) == 1
    assert fetched is not None
    assert fetched.task_attempt is not None
    assert fetched.task_attempt.status == "completed"
    assert fetched.task_attempt.usage == {"tokens": 42}
    assert fetched.session["stats"] == {"tokens": 42}
    assert fetched.session["entries"][0]["authorization"] == "[redacted]"
    assert "sk-abcdefghijklmnopqrstuv" not in str(fetched.session)
    assert fetched.workspace_diff_truncated is True
    assert fetched.workspace_diff is not None
    assert len(fetched.workspace_diff.encode()) <= 120


async def test_list_task_session_archives_distinguishes_missing_task(
    session_factory,
) -> None:
    async with session_factory() as session:
        assert await list_task_session_archives(session, "missing") is None
        assert await get_session_archive(session, "missing") is None


def test_workspace_diff_includes_untracked_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    tracked = workspace / "tracked.txt"
    tracked.write_text("tracked\n")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "-m", "initial")
    (workspace / "new.txt").write_text("untracked content\n")

    diff, truncated = _capture_workspace_diff(str(workspace))

    assert truncated is False
    assert diff is not None
    assert "new.txt" in diff
    assert "+untracked content" in diff
