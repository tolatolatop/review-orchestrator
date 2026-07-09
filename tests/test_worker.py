from pathlib import Path

import pytest

from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.github import GitHubClientError
from review_orchestrator.models import AgentTask, Finding, PullRequestContext, ReviewRun
from review_orchestrator.openhands import (
    OpenHandsConversation,
    OpenHandsEventPage,
    OpenHandsStartTask,
    OpenHandsStartTaskStatus,
)
from review_orchestrator.review_results import parse_review_result
from review_orchestrator.schemas import ReviewRunCreate
from review_orchestrator.services import create_review_run
from review_orchestrator.worker import (
    acquire_next_review_run,
    emit_timeout_event,
    process_next_agent_task,
    process_next_review_run,
    release_review_run_lock,
)


class FakeOpenHandsClient:
    def __init__(self) -> None:
        self.events = OpenHandsEventPage(items=[])
        self.start_task = OpenHandsStartTask(
            id="task-1",
            status=OpenHandsStartTaskStatus.ready,
            app_conversation_id="conversation-1",
            sandbox_id="sandbox-1",
            agent_server_url="http://agent-server",
        )
        self.conversation = OpenHandsConversation(
            id="conversation-1",
            sandbox_status="RUNNING",
            execution_status="RUNNING",
        )

    async def start_conversation(self, review_input):
        return self.start_task

    async def get_start_task(self, task_id: str):
        return self.start_task

    async def get_conversation(self, conversation_id: str):
        return self.conversation

    async def list_events(self, conversation_id: str, *, page_id=None, limit=100):
        return self.events


class ResultOpenHandsClient(FakeOpenHandsClient):
    def __init__(self) -> None:
        super().__init__()
        self.events = OpenHandsEventPage(
            items=[
                {
                    "message": {
                        "content": {
                            "text": (
                                '{"summary":"Done.",'
                                '"findings":[{"file":"src/app.py","line":2,'
                                '"severity":"high","message":"Bug.",'
                                '"confidence":0.9}]}'
                            )
                        }
                    }
                }
            ]
        )


class FakeGitHubClient:
    async def get_pull_request(self, repo_full_name: str, pull_request_number: int):
        return {
            "id": 2002,
            "number": pull_request_number,
            "title": "Improve review",
            "state": "open",
            "html_url": f"https://github.com/{repo_full_name}/pull/{pull_request_number}",
            "user": {"login": "alice"},
            "base": {
                "ref": "main",
                "sha": "a" * 40,
                "repo": {"full_name": repo_full_name},
            },
            "head": {
                "ref": "feature",
                "sha": "b" * 40,
                "repo": {"full_name": repo_full_name},
            },
        }

    async def list_pull_request_files(self, repo_full_name, pull_request_number):
        return []

    async def list_issue_comments(self, repo_full_name, pull_request_number):
        return []

    async def create_issue_comment(self, repo_full_name, pull_request_number, body):
        return "summary-1"

    async def update_issue_comment(self, repo_full_name, comment_id, body):
        return comment_id


class FailingChangedFilesGitHubClient(FakeGitHubClient):
    async def list_pull_request_files(self, repo_full_name, pull_request_number):
        raise GitHubClientError("permission denied")


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


async def test_worker_acquires_and_releases_review_run_lock(session_factory) -> None:
    async with session_factory() as session:
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )

        acquired = await acquire_next_review_run(session, worker_id="worker-1")
        assert acquired is not None
        assert acquired.id == review_run.id
        assert acquired.status == "running"
        assert acquired.lock_owner == "worker-1"
        assert acquired.locked_until is not None

        released = await release_review_run_lock(session, acquired.id)
        assert released is not None
        assert released.lock_owner is None
        assert released.locked_until is None


async def test_timeout_events_are_emitted_once(session_factory) -> None:
    async with session_factory() as session:
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        await acquire_next_review_run(session, worker_id="worker-1")

        soft = await emit_timeout_event(session, review_run.id, timeout_kind="soft")
        duplicate_soft = await emit_timeout_event(
            session, review_run.id, timeout_kind="soft"
        )
        hard = await emit_timeout_event(session, review_run.id, timeout_kind="hard")

        assert soft is not None
        assert duplicate_soft is not None
        assert duplicate_soft.id == soft.id
        assert hard is not None
        assert hard.internal_event == "review_run.hard_timeout"

        refreshed = await session.get(type(review_run), review_run.id)
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.failure_code == "hard_timeout"


async def test_reconciliation_marks_new_existing_and_resolved(session_factory) -> None:
    from review_orchestrator.reconciliation import persist_and_reconcile_findings

    async with session_factory() as session:
        first_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        first_result = parse_review_result(
            {
                "summary": "Two findings.",
                "findings": [
                    {
                        "file": "src/auth.py",
                        "line": 42,
                        "severity": "high",
                        "message": "Token expiry is ignored.",
                        "confidence": 0.9,
                    },
                    {
                        "file": "src/api.py",
                        "line": 10,
                        "severity": "medium",
                        "message": "Error response lacks context.",
                        "confidence": 0.8,
                    },
                ],
            },
            provider="github",
            repo_full_name="example/repo",
            pr_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )
        first_stats = await persist_and_reconcile_findings(
            session, first_run, first_result
        )
        first_run.status = "completed"
        session.add(first_run)
        await session.commit()

        second_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="c" * 40,
            ),
        )
        second_result = parse_review_result(
            {
                "summary": "Two findings.",
                "findings": [
                    {
                        "file": "src/auth.py",
                        "line": 42,
                        "severity": "high",
                        "message": "Token expiry is ignored.",
                        "confidence": 0.9,
                    },
                    {
                        "file": "src/cache.py",
                        "line": 25,
                        "severity": "low",
                        "message": "Cache timeout is undocumented.",
                        "confidence": 0.7,
                    },
                ],
            },
            provider="github",
            repo_full_name="example/repo",
            pr_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )

        second_stats = await persist_and_reconcile_findings(
            session, second_run, second_result
        )

    assert first_stats.new == 2
    assert second_stats.existing == 1
    assert second_stats.new == 1
    assert second_stats.resolved == 1


async def test_comment_refs_upsert_summary_and_dedupe_line_comments(
    session_factory,
) -> None:
    from review_orchestrator.comments import (
        build_summary_comment_body,
        ensure_line_comment_ref,
        upsert_summary_comment_ref,
    )

    async with session_factory() as session:
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        body = build_summary_comment_body(
            review_run,
            status_text="completed",
            finding_stats={"new": 1},
        )
        summary_ref = await upsert_summary_comment_ref(
            session,
            review_run,
            provider_comment_id="summary-1",
            body=body,
        )
        updated_ref = await upsert_summary_comment_ref(
            session,
            review_run,
            provider_comment_id="summary-1",
            body=body + "\nupdated",
        )

        finding = Finding(
            review_run_id=review_run.id,
            fingerprint="finding-1",
            file_path="src/app.py",
            line_start=12,
            severity="high",
            message="Auth check is skipped.",
        )
        session.add(finding)
        await session.commit()
        await session.refresh(finding)

        first_line_ref, first_created = await ensure_line_comment_ref(
            session,
            review_run,
            finding,
            provider_comment_id="line-1",
            body="line body",
        )
        second_line_ref, second_created = await ensure_line_comment_ref(
            session,
            review_run,
            finding,
            provider_comment_id="line-2",
            body="line body",
        )

    assert updated_ref.id == summary_ref.id
    assert first_created is True
    assert second_created is False
    assert second_line_ref.id == first_line_ref.id


async def test_agent_task_worker_creates_review_run_for_mention(
    session_factory,
) -> None:
    async with session_factory() as session:
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
        task = AgentTask(
            pull_request_context_id=context.id,
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            task_type="mention",
            status="queued",
        )
        session.add(task)
        await session.commit()

        processed = await process_next_agent_task(session, worker_id="worker-1")

    assert processed is not None
    assert processed.status == "completed"
    assert processed.result_json["review_run_id"]


async def test_agent_task_worker_hydrates_context_for_first_mention(
    session_factory,
) -> None:
    async with session_factory() as session:
        task = AgentTask(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            task_type="mention",
            status="queued",
        )
        session.add(task)
        await session.commit()

        processed = await process_next_agent_task(
            session,
            worker_id="worker-1",
            github_client=FakeGitHubClient(),
        )

    assert processed is not None
    assert processed.status == "completed"
    assert processed.pull_request_context_id is not None


async def test_review_worker_releases_lock_when_openhands_result_not_ready(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
    )
    async with session_factory() as session:
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
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        review_run.pull_request_context_id = context.id
        review_run.workspace_path = str(tmp_path / "existing-workspace")
        session.add(review_run)
        await session.commit()

        processed = await process_next_review_run(
            session,
            settings=settings,
            openhands_client=FakeOpenHandsClient(),
            worker_id="worker-1",
        )

    assert processed is not None
    assert processed.status == "running"
    assert processed.stage == "waiting_for_result"
    assert processed.lock_owner is None
    assert processed.locked_until is not None


async def test_waiting_review_run_backs_off_and_queued_run_is_prioritized(
    session_factory,
) -> None:
    async with session_factory() as session:
        waiting = ReviewRun(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=41,
            base_sha="a" * 40,
            head_sha="b" * 40,
            status="running",
            stage="waiting_for_result",
            locked_until=utc_future(),
        )
        queued = ReviewRun(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            base_sha="a" * 40,
            head_sha="c" * 40,
            status="queued",
        )
        session.add_all([waiting, queued])
        await session.commit()

        acquired = await acquire_next_review_run(session, worker_id="worker-1")

    assert acquired is not None
    assert acquired.id == queued.id


async def test_changed_files_failure_degrades_to_summary_only_collection(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
    )
    async with session_factory() as session:
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
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider="github",
                repo_full_name="example/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        review_run.pull_request_context_id = context.id
        review_run.workspace_path = str(tmp_path / "existing-workspace")
        session.add(review_run)
        await session.commit()

        processed = await process_next_review_run(
            session,
            settings=settings,
            openhands_client=ResultOpenHandsClient(),
            worker_id="worker-1",
            github_client=FailingChangedFilesGitHubClient(),
        )

    assert processed is not None
    assert processed.status == "completed"
    assert processed.review_summary == "Done."
    assert (
        processed.validation_warnings_json[0]["code"]
        == "changed_files_lookup_failed"
    )


def utc_future():
    from datetime import timedelta

    from review_orchestrator.models import utc_now

    return utc_now() + timedelta(minutes=5)
