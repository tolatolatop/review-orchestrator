from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from review_orchestrator.application.delivery import process_next_delivery
from review_orchestrator.comments import (
    agent_task_marker,
    publish_github_agent_task_comment,
)
from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.github import GitHubAdapter, GitHubClientError
from review_orchestrator.models import AgentTask, PullRequestContext, ReviewRun, utc_now
from review_orchestrator.pi_agent import PiAgentClientError, PiAgentSession
from review_orchestrator.providers import ProviderRegistry
from review_orchestrator.worker import (
    process_agent_task_timeouts,
    process_next_agent_task,
)


def github_registry(client) -> ProviderRegistry:
    return ProviderRegistry([GitHubAdapter(client)])


class FakeGitHubClient:
    def __init__(self) -> None:
        self.issue_comments: list[object] = []
        self.operations: list[tuple[str, str]] = []

    async def list_issue_comments(self, repo_full_name, pull_request_number):
        del repo_full_name, pull_request_number
        return self.issue_comments

    async def create_issue_comment(self, repo_full_name, pull_request_number, body):
        del repo_full_name
        comment_id = f"task-comment-{len(self.issue_comments) + 1}"
        self.issue_comments.append(
            type(
                "Comment",
                (),
                {
                    "id": comment_id,
                    "body": body,
                    "pull_request_number": pull_request_number,
                },
            )
        )
        self.operations.append(("create", body))
        return comment_id

    async def update_issue_comment(self, repo_full_name, comment_id, body):
        del repo_full_name
        for comment in self.issue_comments:
            if str(comment.id) == str(comment_id):
                comment.body = body
                break
        self.operations.append(("update", body))
        return comment_id


class CompletedInstructionClient:
    def __init__(self, github_client: FakeGitHubClient) -> None:
        self.github_client = github_client
        self.started_inputs = []
        self.cancelled_session_ids: list[str] = []
        self.session: PiAgentSession | None = None

    async def start_instruction_session(self, instruction, **kwargs):
        assert len(self.github_client.issue_comments) == 1
        assert (
            "Working on @alice's request"
            in self.github_client.issue_comments[0].body
        )
        self.started_inputs.append((instruction, kwargs))
        self.session = PiAgentSession(
            id="instruction-session-1",
            kind="instruction",
            status="completed",
            stage="completed",
            provider="openai",
            model="gpt-5.4",
            thinking_level="high",
            result={
                "outcome": "answered",
                "answer": "The retry is bounded by two attempts.",
                "references": [
                    {"path": "src/retry.py", "line_start": 10, "line_end": 18}
                ],
            },
        )
        return self.session

    async def get_session(self, session_id: str):
        assert self.session is not None
        assert session_id == self.session.id
        return self.session

    async def cancel_session(self, session_id: str):
        self.cancelled_session_ids.append(session_id)
        return PiAgentSession(
            id=session_id,
            kind="instruction",
            status="cancelled",
            stage="cancelled",
        )


class RunningInstructionClient(CompletedInstructionClient):
    async def start_instruction_session(self, instruction, **kwargs):
        assert len(self.github_client.issue_comments) == 1
        self.started_inputs.append((instruction, kwargs))
        return PiAgentSession(
            id="instruction-session-running",
            kind="instruction",
            status="running",
            stage="thinking",
            provider="openai",
            model="gpt-5.4",
            thinking_level="high",
        )


class InvalidInstructionResultClient(CompletedInstructionClient):
    async def start_instruction_session(self, instruction, **kwargs):
        assert len(self.github_client.issue_comments) == 1
        return PiAgentSession(
            id="instruction-session-invalid",
            kind="instruction",
            status="completed",
            stage="completed",
            result={
                "answer": "SENSITIVE UNVALIDATED MODEL OUTPUT",
                "references": "not-an-array",
            },
        )


class FailingInstructionClient(CompletedInstructionClient):
    async def start_instruction_session(self, instruction, **kwargs):
        assert len(self.github_client.issue_comments) == 1
        raise PiAgentClientError("runtime unavailable", status_code=503)


class FlakyInstructionClient(CompletedInstructionClient):
    def __init__(self, github_client: FakeGitHubClient) -> None:
        super().__init__(github_client)
        self.idempotency_keys: list[str] = []

    async def start_instruction_session(self, instruction, **kwargs):
        self.idempotency_keys.append(instruction.idempotency_key)
        if len(self.idempotency_keys) == 1:
            raise PiAgentClientError("runtime unavailable", status_code=503)
        return await super().start_instruction_session(instruction, **kwargs)


class FailingOnceUpdateGitHubClient(FakeGitHubClient):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def update_issue_comment(self, repo_full_name, comment_id, body):
        if "🤖 Answer" in body and not self.failed:
            self.failed = True
            self.operations.append(("update_failed", body))
            raise GitHubClientError("temporary provider failure")
        return await super().update_issue_comment(repo_full_name, comment_id, body)


class FailingOnceFailureUpdateGitHubClient(FakeGitHubClient):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def update_issue_comment(self, repo_full_name, comment_id, body):
        if "Request for @alice failed" in body and not self.failed:
            self.failed = True
            self.operations.append(("update_failed", body))
            raise GitHubClientError("temporary failure publish error")
        return await super().update_issue_comment(repo_full_name, comment_id, body)


class FailingOnceCreateGitHubClient(FakeGitHubClient):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def create_issue_comment(self, repo_full_name, pull_request_number, body):
        if not self.failed:
            self.failed = True
            self.operations.append(("create_failed", body))
            raise GitHubClientError("temporary placeholder error")
        return await super().create_issue_comment(
            repo_full_name,
            pull_request_number,
            body,
        )


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


def command_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "agent_task_soft_timeout_seconds": 10,
        "agent_task_timeout_seconds": 20,
        "retry_max_attempts": 1,
    }
    values.update(overrides)
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
        **values,
    )


async def add_command_task(
    session,
    tmp_path: Path,
    *,
    status: str = "queued",
    stage: str = "placeholder_pending",
) -> AgentTask:
    context = PullRequestContext(
        provider="github",
        repo_full_name="example/repo",
        pull_request_number=42,
        base_sha="a" * 40,
        head_sha="b" * 40,
        status="open",
    )
    session.add(context)
    await session.commit()
    await session.refresh(context)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    source = workspace / "src"
    source.mkdir(exist_ok=True)
    (source / "retry.py").write_text(
        "def retry():\n    return True\n",
        encoding="utf-8",
    )
    task = AgentTask(
        pull_request_context_id=context.id,
        provider="github",
        repo_full_name="example/repo",
        pull_request_number=42,
        task_type="message_command",
        status=status,
        stage=stage,
        source_kind="issue_comment",
        source_comment_id="123",
        source_url="https://github.com/example/repo/pull/42#issuecomment-123",
        source_author_login="alice",
        command_text="Explain why this retry is safe.",
        head_sha="b" * 40,
        workspace_path=str(workspace),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _deliver_once(session, github_client, *, worker_id="publisher-1"):
    return await process_next_delivery(
        session,
        worker_id=worker_id,
        provider_registry=github_registry(github_client),
        retry_delay_seconds=0,
    )


async def _run_command_until(
    session,
    *,
    task_id: str,
    settings: Settings,
    pi_agent_client,
    github_client,
    statuses: set[str],
    max_cycles: int = 10,
):
    for index in range(max_cycles):
        await _deliver_once(
            session,
            github_client,
            worker_id=f"publisher-{index}-before",
        )
        await process_next_agent_task(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            worker_id=f"worker-{index}",
            provider_registry=github_registry(github_client),
        )
        await _deliver_once(
            session,
            github_client,
            worker_id=f"publisher-{index}-after",
        )
        task = await session.get(AgentTask, task_id)
        assert task is not None
        if task.status in statuses:
            return task
    raise AssertionError(f"Task {task_id} did not reach {statuses}.")


async def test_command_task_publishes_placeholder_before_agent_and_updates_it(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FakeGitHubClient()
    pi_agent_client = CompletedInstructionClient(github_client)
    async with session_factory() as session:
        task = await add_command_task(session, tmp_path)

        processed = await _run_command_until(
            session,
            task_id=task.id,
            settings=command_settings(tmp_path),
            pi_agent_client=pi_agent_client,
            github_client=github_client,
            statuses={"completed"},
        )
        review_runs = list((await session.execute(select(ReviewRun))).scalars())

    assert processed is not None
    assert processed.id == task.id
    assert processed.status == "completed"
    assert processed.stage == "completed"
    assert processed.result_text == "The retry is bounded by two attempts."
    assert processed.response_comment_id == "task-comment-1"
    assert review_runs == []
    assert [operation for operation, _ in github_client.operations] == [
        "create",
        "update",
    ]
    assert len(github_client.issue_comments) == 1
    assert "🤖 Answer for @alice" in github_client.issue_comments[0].body
    assert "`src/retry.py:10-18`" in github_client.issue_comments[0].body
    _, runtime_options = pi_agent_client.started_inputs[0]
    selected_preset = runtime_options["preset"]
    assert selected_preset.resource.name == "default-agent-task"
    assert selected_preset.resource.revision == 1


async def test_empty_command_returns_guidance_without_starting_agent(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FakeGitHubClient()
    pi_agent_client = CompletedInstructionClient(github_client)
    async with session_factory() as session:
        task = await add_command_task(session, tmp_path)
        task.command_text = ""
        await session.commit()

        completed = await _run_command_until(
            session,
            task_id=task.id,
            settings=command_settings(tmp_path),
            pi_agent_client=pi_agent_client,
            github_client=github_client,
            statuses={"completed"},
        )

    assert completed is not None
    assert completed.status == "completed"
    assert completed.result_json["outcome"] == "needs_clarification"
    assert pi_agent_client.started_inputs == []
    assert len(github_client.issue_comments) == 1
    assert "Please include a request" in github_client.issue_comments[0].body


async def test_placeholder_is_recovered_by_exact_task_marker_after_crash(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FakeGitHubClient()
    async with session_factory() as session:
        task = await add_command_task(session, tmp_path)
        github_client.issue_comments.append(
            type(
                "Comment",
                (),
                {"id": "recovered-1", "body": agent_task_marker(task)},
            )
        )

        comment_id = await publish_github_agent_task_comment(
            session,
            task,
            github_client=github_client,
            state="working",
        )

    assert comment_id == "recovered-1"
    assert task.response_comment_id == "recovered-1"
    assert len(github_client.issue_comments) == 1
    assert [operation for operation, _ in github_client.operations] == ["update"]


async def test_agent_does_not_start_until_placeholder_retry_succeeds(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FailingOnceCreateGitHubClient()
    pi_agent_client = CompletedInstructionClient(github_client)
    settings = command_settings(tmp_path, worker_poll_interval_seconds=0.001)
    async with session_factory() as session:
        task = await add_command_task(session, tmp_path)

        waiting = await process_next_agent_task(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        assert waiting is not None
        waiting_state = (waiting.status, waiting.stage)
        assert pi_agent_client.started_inputs == []
        failed_delivery = await _deliver_once(session, github_client)
        assert failed_delivery is not None
        assert failed_delivery.status == "queued"
        completed = await _run_command_until(
            session,
            task_id=task.id,
            settings=settings,
            pi_agent_client=pi_agent_client,
            github_client=github_client,
            statuses={"completed"},
        )

    assert waiting_state == ("awaiting_delivery", "placeholder_delivery_pending")
    assert completed is not None
    assert completed.status == "completed"
    assert len(pi_agent_client.started_inputs) == 1
    assert len(github_client.issue_comments) == 1
    assert [operation for operation, _ in github_client.operations] == [
        "create_failed",
        "create",
        "update",
    ]


async def test_command_task_includes_bounded_completed_history(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FakeGitHubClient()
    pi_agent_client = CompletedInstructionClient(github_client)
    async with session_factory() as session:
        current = await add_command_task(session, tmp_path)
        previous = AgentTask(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            task_type="message_command",
            status="completed",
            stage="completed",
            source_author_login="alice",
            command_text="Where is retry configured?",
            result_text="In src/retry.py.",
            result_json={"outcome": "answered", "answer": "In src/retry.py."},
            head_sha="b" * 40,
            completed_at=utc_now() - timedelta(minutes=1),
            created_at=current.created_at - timedelta(minutes=2),
        )
        session.add(previous)
        await session.commit()

        await _run_command_until(
            session,
            task_id=current.id,
            settings=command_settings(tmp_path),
            pi_agent_client=pi_agent_client,
            github_client=github_client,
            statuses={"completed"},
        )

    instruction, _ = pi_agent_client.started_inputs[0]
    assert len(instruction.history) == 1
    assert instruction.history[0].command == "Where is retry configured?"
    assert instruction.history[0].answer == "In src/retry.py."


async def test_newer_command_gets_placeholder_but_waits_for_older_pr_task(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FakeGitHubClient()
    pi_agent_client = CompletedInstructionClient(github_client)
    async with session_factory() as session:
        current = await add_command_task(session, tmp_path)
        older = AgentTask(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            task_type="message_command",
            status="running",
            stage="waiting_for_agent",
            command_text="Older command",
            source_author_login="bob",
            created_at=current.created_at - timedelta(minutes=1),
            locked_until=utc_now() + timedelta(minutes=1),
        )
        session.add(older)
        await session.commit()

        await process_next_agent_task(
            session,
            settings=command_settings(tmp_path),
            pi_agent_client=pi_agent_client,
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await _deliver_once(session, github_client)
        waiting = await process_next_agent_task(
            session,
            settings=command_settings(tmp_path),
            pi_agent_client=pi_agent_client,
            worker_id="worker-2",
            provider_registry=github_registry(github_client),
        )
        await _deliver_once(session, github_client)

    assert waiting is not None
    assert waiting.id == current.id
    assert waiting.status == "queued"
    assert waiting.stage == "waiting_for_turn"
    assert pi_agent_client.started_inputs == []
    assert len(github_client.issue_comments) == 1
    assert "Status: queued" in github_client.issue_comments[0].body
    assert [operation for operation, _ in github_client.operations] == [
        "create",
        "update",
    ]


async def test_command_runtime_failure_updates_existing_placeholder(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FakeGitHubClient()
    pi_agent_client = FailingInstructionClient(github_client)
    async with session_factory() as session:
        task = await add_command_task(session, tmp_path)

        processed = await _run_command_until(
            session,
            task_id=task.id,
            settings=command_settings(tmp_path),
            pi_agent_client=pi_agent_client,
            github_client=github_client,
            statuses={"failed"},
        )

    assert processed is not None
    assert processed.status == "failed"
    assert processed.failure_code == "agent_start_failed"
    assert len(github_client.issue_comments) == 1
    assert [operation for operation, _ in github_client.operations] == [
        "create",
        "update",
    ]
    assert "Could not complete the request" in github_client.issue_comments[0].body


async def test_invalid_agent_result_is_not_copied_into_failure_comment(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FakeGitHubClient()
    pi_agent_client = InvalidInstructionResultClient(github_client)
    async with session_factory() as session:
        task = await add_command_task(session, tmp_path)

        processed = await _run_command_until(
            session,
            task_id=task.id,
            settings=command_settings(tmp_path),
            pi_agent_client=pi_agent_client,
            github_client=github_client,
            statuses={"failed"},
        )

    assert processed is not None
    assert processed.status == "failed"
    assert processed.failure_code == "invalid_result"
    assert len(github_client.issue_comments) == 1
    body = github_client.issue_comments[0].body
    assert "Failure category: `invalid_result`" in body
    assert "SENSITIVE UNVALIDATED MODEL OUTPUT" not in body


async def test_command_retries_infrastructure_start_with_same_idempotency_key(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FakeGitHubClient()
    pi_agent_client = FlakyInstructionClient(github_client)
    settings = command_settings(
        tmp_path,
        retry_max_attempts=2,
        retry_initial_delay_seconds=0,
    )
    async with session_factory() as session:
        task = await add_command_task(session, tmp_path)

        await process_next_agent_task(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await _deliver_once(session, github_client)
        retrying = await process_next_agent_task(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            worker_id="worker-2",
            provider_registry=github_registry(github_client),
        )
        retrying_state = (retrying.status, retrying.stage) if retrying else None
        completed = await _run_command_until(
            session,
            task_id=task.id,
            settings=settings,
            pi_agent_client=pi_agent_client,
            github_client=github_client,
            statuses={"completed"},
        )

    assert retrying is not None
    assert retrying_state == ("queued", "retrying_agent_start")
    assert completed is not None
    assert completed.status == "completed"
    assert pi_agent_client.idempotency_keys == [
        f"agent-task:{completed.id}:attempt:1",
        f"agent-task:{completed.id}:attempt:1",
    ]
    assert len(github_client.issue_comments) == 1


async def test_final_comment_publish_retries_without_rerunning_agent(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FailingOnceUpdateGitHubClient()
    pi_agent_client = CompletedInstructionClient(github_client)
    settings = command_settings(tmp_path, worker_poll_interval_seconds=0.001)
    async with session_factory() as session:
        task = await add_command_task(session, tmp_path)

        await process_next_agent_task(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await _deliver_once(session, github_client)
        publishing = await process_next_agent_task(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            worker_id="worker-2",
            provider_registry=github_registry(github_client),
        )
        publishing_state = (
            (publishing.status, publishing.stage) if publishing else None
        )
        failed_delivery = await _deliver_once(session, github_client)
        assert failed_delivery is not None
        assert failed_delivery.status == "queued"
        completed = await _run_command_until(
            session,
            task_id=task.id,
            settings=settings,
            pi_agent_client=pi_agent_client,
            github_client=github_client,
            statuses={"completed"},
        )

    assert publishing_state == ("awaiting_delivery", "result_delivery_pending")
    assert completed is not None
    assert completed.status == "completed"
    assert len(pi_agent_client.started_inputs) == 1
    assert len(github_client.issue_comments) == 1
    assert [operation for operation, _ in github_client.operations] == [
        "create",
        "update_failed",
        "update",
    ]


async def test_failure_comment_publish_retries_until_placeholder_is_terminal(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FailingOnceFailureUpdateGitHubClient()
    pi_agent_client = FailingInstructionClient(github_client)
    settings = command_settings(tmp_path, worker_poll_interval_seconds=0.001)
    async with session_factory() as session:
        task = await add_command_task(session, tmp_path)

        await process_next_agent_task(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await _deliver_once(session, github_client)
        publishing = await process_next_agent_task(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            worker_id="worker-2",
            provider_registry=github_registry(github_client),
        )
        publishing_state = (
            (publishing.status, publishing.stage) if publishing else None
        )
        failed_delivery = await _deliver_once(session, github_client)
        assert failed_delivery is not None
        assert failed_delivery.status == "queued"
        failed = await _run_command_until(
            session,
            task_id=task.id,
            settings=settings,
            pi_agent_client=pi_agent_client,
            github_client=github_client,
            statuses={"failed"},
        )

    assert publishing_state == ("awaiting_delivery", "failure_delivery_pending")
    assert failed is not None
    assert failed.status == "failed"
    assert failed.stage == "failed"
    assert len(github_client.issue_comments) == 1
    assert "Request for @alice failed" in github_client.issue_comments[0].body
    assert [operation for operation, _ in github_client.operations] == [
        "create",
        "update_failed",
        "update",
    ]


async def test_command_soft_and_hard_timeout_refresh_same_placeholder(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FakeGitHubClient()
    pi_agent_client = RunningInstructionClient(github_client)
    settings = command_settings(tmp_path)
    async with session_factory() as session:
        await add_command_task(session, tmp_path)
        await process_next_agent_task(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await _deliver_once(session, github_client)
        running = await process_next_agent_task(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            worker_id="worker-2",
            provider_registry=github_registry(github_client),
        )
        assert running is not None
        assert running.status == "running"
        started_at = running.started_at
        assert started_at is not None

        soft = await process_agent_task_timeouts(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            provider_registry=github_registry(github_client),
            now=started_at + timedelta(seconds=11),
        )
        await _deliver_once(session, github_client)
        hard = await process_agent_task_timeouts(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            provider_registry=github_registry(github_client),
            now=started_at + timedelta(seconds=21),
        )
        await _deliver_once(session, github_client)
        await session.refresh(running)

    assert len(soft) == 1
    assert soft[0].soft_timeout_emitted_at is not None
    assert len(hard) == 1
    assert hard[0].status == "failed"
    assert hard[0].failure_code == "hard_timeout"
    assert pi_agent_client.cancelled_session_ids == ["instruction-session-running"]
    assert len(github_client.issue_comments) == 1
    assert "timed out" in github_client.issue_comments[0].body.lower()
    assert [operation for operation, _ in github_client.operations] == [
        "create",
        "update",
        "update",
    ]


async def test_hard_timeout_does_not_discard_result_waiting_for_provider_publish(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FakeGitHubClient()
    pi_agent_client = RunningInstructionClient(github_client)
    settings = command_settings(tmp_path)
    async with session_factory() as session:
        task = await add_command_task(
            session,
            tmp_path,
            status="awaiting_delivery",
            stage="result_delivery_pending",
        )
        task.execution_status = "completed"
        task.started_at = utc_now() - timedelta(minutes=10)
        task.agent_session_id = "completed-session"
        task.result_text = "Validated answer waiting for GitHub."
        task.result_json = {
            "outcome": "answered",
            "answer": task.result_text,
            "references": [],
        }
        await session.commit()

        touched = await process_agent_task_timeouts(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            provider_registry=github_registry(github_client),
            now=utc_now(),
        )

    assert touched == []
    assert task.status == "awaiting_delivery"
    assert task.stage == "result_delivery_pending"
    assert task.failure_code is None
    assert pi_agent_client.cancelled_session_ids == []


async def test_pending_pr_close_cancellation_updates_task_placeholder(
    session_factory,
    tmp_path: Path,
) -> None:
    github_client = FakeGitHubClient()
    pi_agent_client = RunningInstructionClient(github_client)
    async with session_factory() as session:
        task = await add_command_task(
            session,
            tmp_path,
            status="running",
            stage="cancellation_pending",
        )
        task.agent_session_id = "instruction-session-running"
        task.response_comment_id = "task-comment-1"
        github_client.issue_comments.append(
            type(
                "Comment",
                (),
                {"id": "task-comment-1", "body": "working"},
            )
        )
        await session.commit()

        cancelled = await process_next_agent_task(
            session,
            settings=command_settings(tmp_path),
            pi_agent_client=pi_agent_client,
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await _deliver_once(session, github_client)
        await session.refresh(cancelled)

    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert cancelled.stage == "cancelled"
    assert pi_agent_client.cancelled_session_ids == ["instruction-session-running"]
    assert len(github_client.issue_comments) == 1
    assert "cancelled" in github_client.issue_comments[0].body
