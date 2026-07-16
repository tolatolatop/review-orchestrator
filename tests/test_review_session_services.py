from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.application.services import (
    ReviewRunTransitionError,
    cancel_review_session,
    collect_review_result,
    get_pi_agent_session_diagnostics_for_agent_task,
    get_pi_agent_session_diagnostics_for_review_run,
    get_pi_agent_session_diagnostics_for_session,
    start_review_session,
    sync_review_session,
)
from review_orchestrator.domain.models import AgentTask, ReviewCommentSlot
from review_orchestrator.infrastructure.config import Settings
from review_orchestrator.infrastructure.db import (
    create_engine,
    create_session_factory,
    init_models,
)
from review_orchestrator.integrations.pi_agent import (
    PiAgentClientError,
    PiAgentSession,
)
from tests.factories import ReviewRunCreate, create_review_run


@pytest.fixture
async def service_context(tmp_path) -> AsyncIterator[tuple[AsyncSession, Settings]]:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/review-session.db",
    )
    engine = create_engine(settings)
    await init_models(engine)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        yield session, settings
    await engine.dispose()


class StubPiAgentClient:
    def __init__(self) -> None:
        self.session = PiAgentSession(
            id="session-1",
            status="running",
            stage="analyzing",
            provider="openai",
            model="gpt-5.4",
            thinking_level="high",
        )
        self.start_error: PiAgentClientError | None = None
        self.get_error: PiAgentClientError | None = None
        self.cancel_error: PiAgentClientError | None = None

    async def start_session(self, *_args, **_kwargs) -> PiAgentSession:
        if self.start_error is not None:
            raise self.start_error
        return self.session

    async def get_session(self, session_id: str) -> PiAgentSession:
        assert session_id == self.session.id
        if self.get_error is not None:
            raise self.get_error
        return self.session

    async def cancel_session(self, session_id: str) -> PiAgentSession:
        assert session_id == self.session.id
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.session.model_copy(
            update={"status": "cancelled", "stage": "cancelled"}
        )


async def make_run(
    session: AsyncSession,
    *,
    base_sha: str | None = "a" * 40,
    head_sha: str = "b" * 40,
):
    review_run = await create_review_run(
        session,
        ReviewRunCreate(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            base_sha=base_sha,
            head_sha=head_sha,
        ),
    )
    session.add(
        ReviewCommentSlot(
            review_run_id=review_run.id,
            provider=review_run.provider,
            repo_full_name=review_run.repo_full_name,
            pull_request_number=review_run.pull_request_number,
            head_sha=review_run.head_sha,
            marker=f"review-orchestrator:summary:slot:test-{review_run.id}",
            provider_comment_id=f"placeholder-{review_run.id}",
            status="ready",
        )
    )
    review_run.stage = "placeholder_ready"
    await session.commit()
    await session.refresh(review_run)
    return review_run


@pytest.mark.parametrize("status", ["completed", "cancelled", "superseded"])
async def test_terminal_review_runs_cannot_start(
    service_context: tuple[AsyncSession, Settings],
    status: str,
) -> None:
    session, settings = service_context
    review_run = await make_run(session)
    review_run.status = status
    await session.commit()

    with pytest.raises(ReviewRunTransitionError, match="cannot be started"):
        await start_review_session(
            session,
            review_run,
            pi_agent_client=StubPiAgentClient(),  # type: ignore[arg-type]
            settings=settings,
            workspace_path="/tmp/workspace",
        )


@pytest.mark.parametrize(
    ("base_sha", "workspace_path", "message"),
    [
        ("a" * 40, None, "workspace_path is required"),
        (None, "/tmp/workspace", "base_sha is required"),
    ],
)
async def test_start_requires_commit_range_and_workspace(
    service_context: tuple[AsyncSession, Settings],
    base_sha: str | None,
    workspace_path: str | None,
    message: str,
) -> None:
    session, settings = service_context
    review_run = await make_run(session, base_sha=base_sha)

    with pytest.raises(ReviewRunTransitionError, match=message):
        await start_review_session(
            session,
            review_run,
            pi_agent_client=StubPiAgentClient(),  # type: ignore[arg-type]
            settings=settings,
            workspace_path=workspace_path,
        )


@pytest.mark.parametrize(
    ("status_code", "failure_code"),
    [(401, "pi_agent_error"), (None, "pi_agent_infrastructure_error")],
)
async def test_start_classifies_runtime_failures(
    service_context: tuple[AsyncSession, Settings],
    status_code: int | None,
    failure_code: str,
) -> None:
    session, settings = service_context
    review_run = await make_run(session)
    client = StubPiAgentClient()
    client.start_error = PiAgentClientError(
        "runtime unavailable",
        status_code=status_code,
    )

    result = await start_review_session(
        session,
        review_run,
        pi_agent_client=client,  # type: ignore[arg-type]
        settings=settings,
        workspace_path="/tmp/workspace",
    )

    assert result.status == "failed"
    assert result.failure_code == failure_code
    assert result.error == "runtime unavailable"


@pytest.mark.parametrize(
    ("runtime_status", "runtime_stage", "expected_status", "expected_stage"),
    [
        ("running", "tool:read_file", "running", "tool:read_file"),
        ("waiting_for_input", "waiting", "running", "waiting"),
        ("completed", "completed", "running", "agent_completed"),
        ("cancelled", "cancelled", "cancelled", "cancelled"),
    ],
)
async def test_sync_maps_every_runtime_state(
    service_context: tuple[AsyncSession, Settings],
    runtime_status: str,
    runtime_stage: str,
    expected_status: str,
    expected_stage: str,
) -> None:
    session, settings = service_context
    review_run = await make_run(session)
    client = StubPiAgentClient()
    review_run = await start_review_session(
        session,
        review_run,
        pi_agent_client=client,  # type: ignore[arg-type]
        settings=settings,
        workspace_path="/tmp/workspace",
    )
    client.session = client.session.model_copy(
        update={
            "status": runtime_status,
            "stage": runtime_stage,
            "result": (
                {"summary": "complete", "findings": []}
                if runtime_status == "completed"
                else None
            ),
        }
    )

    result = await sync_review_session(
        session,
        review_run,
        pi_agent_client=client,  # type: ignore[arg-type]
    )

    assert result.status == expected_status
    assert result.stage == expected_stage


async def test_sync_fails_when_session_id_is_missing(
    service_context: tuple[AsyncSession, Settings],
) -> None:
    session, _settings = service_context
    review_run = await make_run(session)

    result = await sync_review_session(
        session,
        review_run,
        pi_agent_client=StubPiAgentClient(),  # type: ignore[arg-type]
    )

    assert result.status == "failed"
    assert result.failure_code == "pi_agent_error"
    assert result.error == "pi-agent session id is missing."


async def test_sync_classifies_runtime_transport_failure(
    service_context: tuple[AsyncSession, Settings],
) -> None:
    session, settings = service_context
    review_run = await make_run(session)
    client = StubPiAgentClient()
    review_run = await start_review_session(
        session,
        review_run,
        pi_agent_client=client,  # type: ignore[arg-type]
        settings=settings,
        workspace_path="/tmp/workspace",
    )
    client.get_error = PiAgentClientError("connection reset")

    result = await sync_review_session(
        session,
        review_run,
        pi_agent_client=client,  # type: ignore[arg-type]
    )

    assert result.status == "failed"
    assert result.failure_code == "pi_agent_infrastructure_error"


async def test_cancel_records_runtime_cleanup_failure(
    service_context: tuple[AsyncSession, Settings],
) -> None:
    session, settings = service_context
    review_run = await make_run(session)
    client = StubPiAgentClient()
    review_run = await start_review_session(
        session,
        review_run,
        pi_agent_client=client,  # type: ignore[arg-type]
        settings=settings,
        workspace_path="/tmp/workspace",
    )
    client.cancel_error = PiAgentClientError("cleanup timeout")

    result = await cancel_review_session(
        session,
        review_run,
        pi_agent_client=client,  # type: ignore[arg-type]
        reason="operator request",
    )

    assert result.status == "cancelled"
    assert result.error == "Cancel requested; pi-agent cleanup failed: cleanup timeout"


async def test_collect_requires_base_sha(
    service_context: tuple[AsyncSession, Settings],
) -> None:
    session, _settings = service_context
    review_run = await make_run(session, base_sha=None)

    with pytest.raises(ReviewRunTransitionError, match="base_sha is required"):
        await collect_review_result(
            session,
            review_run,
            raw_output={"summary": "none", "findings": []},
        )


async def test_review_run_diagnostics_include_runtime_state_and_linked_tasks(
    service_context: tuple[AsyncSession, Settings],
) -> None:
    session, _settings = service_context
    review_run = await make_run(session)
    review_run.agent_session_id = "session-1"
    review_run.agent_provider = "openai"
    review_run.agent_model = "gpt-5.4"
    task = AgentTask(
        provider="github",
        repo_full_name="example/repo",
        pull_request_number=42,
        task_type="mention",
        status="completed",
        result_json={"review_run_id": review_run.id},
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    client = StubPiAgentClient()
    client.session = client.session.model_copy(
        update={
            "status": "waiting_for_input",
            "stage": "waiting_for_runtime",
            "events": [
                {
                    "at": "2026-07-15T00:00:00Z",
                    "type": "tool",
                    "stage": "read_file",
                }
            ],
        }
    )

    diagnostics = await get_pi_agent_session_diagnostics_for_review_run(
        session,
        review_run,
        pi_agent_client=client,  # type: ignore[arg-type]
    )

    assert diagnostics.agent_task_ids == [task.id]
    assert diagnostics.execution_status == "waiting_for_input"
    assert diagnostics.execution_stage == "waiting_for_runtime"
    assert diagnostics.event_count == 1
    assert diagnostics.live_status_available is True


async def test_diagnostics_report_runtime_lookup_failures(
    service_context: tuple[AsyncSession, Settings],
) -> None:
    session, _settings = service_context
    review_run = await make_run(session)
    review_run.agent_session_id = "session-1"
    task = AgentTask(
        provider="github",
        repo_full_name="example/repo",
        pull_request_number=42,
        task_type="message_command",
        status="running",
        agent_session_id="session-1",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    client = StubPiAgentClient()
    client.get_error = PiAgentClientError("runtime lookup failed")

    run_diagnostics = await get_pi_agent_session_diagnostics_for_review_run(
        session,
        review_run,
        pi_agent_client=client,  # type: ignore[arg-type]
    )
    task_diagnostics = await get_pi_agent_session_diagnostics_for_agent_task(
        task,
        pi_agent_client=client,  # type: ignore[arg-type]
    )

    assert run_diagnostics.live_status_available is False
    assert run_diagnostics.live_status_error == "runtime lookup failed"
    assert task_diagnostics.live_status_available is False
    assert task_diagnostics.live_status_error == "runtime lookup failed"


async def test_session_diagnostics_falls_back_to_agent_task(
    service_context: tuple[AsyncSession, Settings],
) -> None:
    session, _settings = service_context
    task = AgentTask(
        provider="github",
        repo_full_name="example/repo",
        pull_request_number=42,
        task_type="message_command",
        status="running",
        agent_session_id="task-session",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    client = StubPiAgentClient()
    client.session = client.session.model_copy(update={"id": "task-session"})

    diagnostics = await get_pi_agent_session_diagnostics_for_session(
        session,
        "task-session",
        pi_agent_client=client,  # type: ignore[arg-type]
    )

    assert diagnostics is not None
    assert diagnostics.review_run_id is None
    assert diagnostics.agent_task_ids == [task.id]
    assert diagnostics.execution_status == "running"
