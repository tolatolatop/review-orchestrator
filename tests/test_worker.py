from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from review_orchestrator.application.delivery import process_next_delivery
from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.github import GitHubAdapter, GitHubClientError
from review_orchestrator.models import (
    AgentTask,
    Finding,
    PullRequestContext,
    ReviewRun,
    utc_now,
)
from review_orchestrator.pi_agent import (
    PiAgentClientError,
    PiAgentSession,
)
from review_orchestrator.providers import ProviderOperationError, ProviderRegistry
from review_orchestrator.review_results import parse_review_result
from review_orchestrator.schemas import WorkspacePrepareResponse
from review_orchestrator.worker import (
    acquire_next_review_run,
    emit_timeout_event,
    process_next_agent_task,
    process_next_review_run,
    process_review_run_timeouts,
    release_review_run_lock,
)
from tests.factories import ReviewRunCreate, create_review_run


def github_registry(client) -> ProviderRegistry:
    return ProviderRegistry([GitHubAdapter(client)])


class FakePiAgentClient:
    def __init__(self) -> None:
        self.cancelled_session_ids: list[str] = []
        self.started_inputs = []
        self.session = PiAgentSession(
            id="session-1",
            status="running",
            stage="analyzing",
            provider="openai",
            model="gpt-5.4",
            thinking_level="high",
        )

    async def start_session(self, review_input, **kwargs):
        self.started_inputs.append((review_input, kwargs))
        return self.session

    async def get_session(self, session_id: str):
        assert session_id == self.session.id
        return self.session

    async def cancel_session(self, session_id: str):
        self.cancelled_session_ids.append(session_id)
        self.session = self.session.model_copy(
            update={"status": "cancelled", "stage": "cancelled"}
        )
        return self.session


class ResultPiAgentClient(FakePiAgentClient):
    def __init__(self) -> None:
        super().__init__()
        self.session = self.session.model_copy(
            update={
                "status": "completed",
                "stage": "completed",
                "result": {
                    "summary": "Done.",
                    "findings": [
                        {
                            "file": "src/app.py",
                            "line": 2,
                            "severity": "high",
                            "message": "Bug.",
                            "confidence": 0.9,
                        }
                    ],
                },
            }
        )


class NoFindingsPiAgentClient(FakePiAgentClient):
    def __init__(self) -> None:
        super().__init__()
        self.session = self.session.model_copy(
            update={
                "status": "completed",
                "stage": "completed",
                "result": {"summary": "No issues found.", "findings": []},
            }
        )


class WaitingForHumanPiAgentClient(FakePiAgentClient):
    def __init__(self) -> None:
        super().__init__()
        self.session = self.session.model_copy(
            update={
                "status": "waiting_for_input",
                "stage": "waiting_for_human",
                "pending_input": {
                    "id": "question-1",
                    "question": "Is this behavior intentional?",
                },
            }
        )


class InvalidResultPiAgentClient(FakePiAgentClient):
    def __init__(self) -> None:
        super().__init__()
        self.session = self.session.model_copy(
            update={
                "status": "completed",
                "stage": "completed",
                "result": {"summary": "bad", "findings": {}},
            }
        )


class StartFailingPiAgentClient(FakePiAgentClient):
    async def start_session(self, review_input, **kwargs):
        raise PiAgentClientError(
            "pi-agent token invalid",
            status_code=401,
        )


class InfrastructureFailingPiAgentClient(FakePiAgentClient):
    async def start_session(self, review_input, **kwargs):
        raise PiAgentClientError(
            "pi-agent runtime request failed: connection refused"
        )


class SessionRequestFailingPiAgentClient(FakePiAgentClient):
    async def get_session(self, session_id: str):
        raise PiAgentClientError(
            "pi-agent runtime request failed: timeout"
        )


class FakeGitHubClient:
    def __init__(self) -> None:
        self.issue_comments = []
        self.updated_issue_comment_count = 0

    async def get_token(self, repo_full_name: str):
        del repo_full_name
        return None

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
        return [
            comment
            for comment in self.issue_comments
            if comment.pull_request_number == pull_request_number
        ]

    async def create_issue_comment(self, repo_full_name, pull_request_number, body):
        comment_id = f"summary-{len(self.issue_comments) + 1}"
        self.issue_comments.append(
            type(
                "Comment",
                (),
                {
                    "id": comment_id,
                    "pull_request_number": pull_request_number,
                    "body": body,
                },
            )
        )
        return comment_id

    async def update_issue_comment(self, repo_full_name, comment_id, body):
        self.updated_issue_comment_count += 1
        for comment in self.issue_comments:
            if str(comment.id) == str(comment_id):
                comment.body = body
                break
        return comment_id


class FailingChangedFilesGitHubClient(FakeGitHubClient):
    async def list_pull_request_files(self, repo_full_name, pull_request_number):
        raise GitHubClientError("permission denied")


class FailingPullRequestGitHubClient(FakeGitHubClient):
    async def get_pull_request(self, repo_full_name: str, pull_request_number: int):
        raise GitHubClientError("GitHub token invalid")


class CustomProviderAdapter:
    provider = "custom"

    def __init__(self, *, fail_context: bool = False) -> None:
        self.fail_context = fail_context
        self.summary_statuses: list[str] = []

    async def get_pull_request_context(self, task: AgentTask) -> PullRequestContext:
        if self.fail_context:
            raise ProviderOperationError(
                "Custom context lookup failed",
                provider=self.provider,
                operation="get_pull_request_context",
            )
        return PullRequestContext(
            provider=self.provider,
            repo_full_name=task.repo_full_name,
            pull_request_number=task.pull_request_number,
            base_sha="a" * 40,
            head_sha="b" * 40,
            status="open",
        )

    async def publish_summary_comment(
        self,
        session,
        review_run,
        *,
        status_text: str,
        finding_stats=None,
    ):
        del session, review_run, finding_stats
        self.summary_statuses.append(status_text)
        return None


class RoutingAdapter:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.summary_statuses: list[str] = []

    async def list_changed_files(self, review_run: ReviewRun):
        assert review_run.provider == self.provider
        return []

    async def publish_summary_comment(
        self,
        session,
        review_run,
        *,
        status_text: str,
        finding_stats=None,
    ):
        del session, finding_stats
        assert review_run.provider == self.provider
        self.summary_statuses.append(status_text)
        return None

    async def publish_line_comments(self, session, review_run, *, changed_files):
        del session, review_run, changed_files
        return {"published": 0, "summary_only": 0, "deduped": 0, "failed": 0}


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


async def deliver_all(
    session,
    *,
    provider_registry: ProviderRegistry,
    max_deliveries: int = 20,
) -> None:
    for index in range(max_deliveries):
        delivered = await process_next_delivery(
            session,
            worker_id=f"test-publisher-{index}",
            provider_registry=provider_registry,
            retry_delay_seconds=0,
        )
        if delivered is None:
            return
    raise AssertionError("Delivery outbox did not drain.")


async def deliver_github_all(session, github_client) -> None:
    await deliver_all(
        session,
        provider_registry=github_registry(github_client),
    )


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
            provider_registry=github_registry(FakeGitHubClient()),
        )

    assert processed is not None
    assert processed.status == "completed"
    assert processed.pull_request_context_id is not None


async def test_agent_task_worker_uses_custom_provider_registry(
    session_factory,
) -> None:
    adapter = CustomProviderAdapter()
    async with session_factory() as session:
        task = AgentTask(
            provider="custom",
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
            provider_registry=ProviderRegistry([adapter]),
        )
        await deliver_all(
            session,
            provider_registry=ProviderRegistry([adapter]),
        )

    assert processed is not None
    assert processed.status == "completed"
    assert processed.pull_request_context_id is not None


async def test_agent_task_provider_failure_without_context_does_not_create_review(
    session_factory,
) -> None:
    adapter = CustomProviderAdapter(fail_context=True)
    async with session_factory() as session:
        task = AgentTask(
            provider="custom",
            repo_full_name="example/repo",
            pull_request_number=42,
            task_type="mention",
            status="queued",
            input_json={"payload": {"pull_request": {"head": {"sha": "b" * 40}}}},
        )
        session.add(task)
        await session.commit()

        processed = await process_next_agent_task(
            session,
            worker_id="worker-1",
            provider_registry=ProviderRegistry([adapter]),
        )
        await deliver_all(
            session,
            provider_registry=ProviderRegistry([adapter]),
        )
        review_runs = list((await session.execute(select(ReviewRun))).scalars())

    assert processed is not None
    assert processed.status == "failed"
    assert processed.error_message == "Custom context lookup failed"
    assert review_runs == []
    assert adapter.summary_statuses == []


async def test_agent_task_hydrate_failure_without_context_does_not_publish_summary(
    session_factory,
) -> None:
    github_client = FailingPullRequestGitHubClient()
    async with session_factory() as session:
        task = AgentTask(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            task_type="mention",
            status="queued",
            input_json={
                "payload": {
                    "pull_request": {
                        "head": {"sha": "b" * 40},
                    }
                }
            },
        )
        session.add(task)
        await session.commit()

        processed = await process_next_agent_task(
            session,
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await deliver_all(
            session,
            provider_registry=github_registry(github_client),
        )
        review_runs = list((await session.execute(select(ReviewRun))).scalars())

    assert processed is not None
    assert processed.status == "failed"
    assert processed.error_message == "GitHub token invalid"
    assert review_runs == []
    assert github_client.issue_comments == []


async def test_review_worker_releases_lock_when_pi_agent_result_not_ready(
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
            pi_agent_client=FakePiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(FakeGitHubClient()),
        )

    assert processed is not None
    assert processed.status == "running"
    assert processed.stage == "analyzing"
    assert processed.lock_owner is None
    assert processed.locked_until is None
    assert processed.available_at is not None


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

        github_client = FailingChangedFilesGitHubClient()
        processed = await process_next_review_run(
            session,
            settings=settings,
            pi_agent_client=ResultPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await deliver_github_all(session, github_client)
        await session.refresh(processed)

    assert processed is not None
    assert processed.status == "completed"
    assert processed.stage == "completed"
    assert processed.review_summary == "Done."
    assert (
        processed.validation_warnings_json[0]["code"]
        == "changed_files_lookup_failed"
    )


async def test_worker_collects_structured_no_findings_result(
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

        github_client = FakeGitHubClient()
        processed = await process_next_review_run(
            session,
            settings=settings,
            pi_agent_client=NoFindingsPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await deliver_github_all(session, github_client)
        await session.refresh(processed)

    assert processed is not None
    assert processed.status == "completed"
    assert processed.review_summary == "No issues found."
    assert processed.finding_count_by_severity == {}


@pytest.mark.parametrize("provider", ["gitlab", "forge"])
async def test_review_uses_matching_provider_for_result_upload(
    session_factory,
    tmp_path: Path,
    monkeypatch,
    provider: str,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
    )
    adapter = RoutingAdapter(provider)
    provider_registry = ProviderRegistry([adapter])
    pi_agent = NoFindingsPiAgentClient()
    workspace_path = str(tmp_path / f"{provider}-workspace")

    async def prepare_provider_workspace(
        session,
        supplied_settings,
        request,
        *,
        provider_registry,
    ):
        del session
        assert supplied_settings is settings
        assert request.provider == provider
        assert request.repository.clone_url is None
        assert provider_registry.require(provider) is adapter
        return WorkspacePrepareResponse(
            workspace_id=f"{provider}:workspace",
            workspace_path=workspace_path,
            base_sha=request.pull_request.base_sha,
            head_sha=request.pull_request.head_sha,
            status="ready",
        )

    monkeypatch.setattr(
        "review_orchestrator.worker.prepare_workspace",
        prepare_provider_workspace,
    )
    async with session_factory() as session:
        context = PullRequestContext(
            provider=provider,
            repo_full_name="group/repo",
            pull_request_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
            status="opened",
        )
        session.add(context)
        await session.commit()
        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider=provider,
                repo_full_name="group/repo",
                pull_request_number=42,
                base_sha="a" * 40,
                head_sha="b" * 40,
            ),
        )
        review_run.pull_request_context_id = context.id
        session.add(review_run)
        await session.commit()

        processed = await process_next_review_run(
            session,
            settings=settings,
            pi_agent_client=pi_agent,
            worker_id="worker-1",
            provider_registry=provider_registry,
        )
        await deliver_all(session, provider_registry=provider_registry)
        await session.refresh(processed)

    assert processed is not None
    assert processed.status == "completed"
    assert adapter.summary_statuses == ["completed"]
    assert pi_agent.started_inputs[0][0].provider == provider
    assert pi_agent.started_inputs[0][0].workspace_path == workspace_path


async def test_worker_collects_structured_result_without_event_scraping(
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

        github_client = FakeGitHubClient()
        processed = await process_next_review_run(
            session,
            settings=settings,
            pi_agent_client=NoFindingsPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await deliver_github_all(session, github_client)
        await session.refresh(processed)

    assert processed is not None
    assert processed.status == "completed"
    assert processed.review_summary == "No issues found."


async def test_review_worker_publishes_failed_summary_on_pi_agent_start_failure(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
    )
    github_client = FakeGitHubClient()
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
            pi_agent_client=StartFailingPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await deliver_github_all(session, github_client)
        await session.refresh(processed)

    assert processed is not None
    assert processed.status == "failed"
    assert "Review status: failed" in github_client.issue_comments[0].body
    assert "Failure category: pi_agent_error" in github_client.issue_comments[0].body
    assert "token [redacted]" in github_client.issue_comments[0].body


async def test_review_worker_retries_transient_pi_agent_start_failure(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
        retry_max_attempts=2,
        retry_initial_delay_seconds=1,
    )
    github_client = FakeGitHubClient()
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
            pi_agent_client=InfrastructureFailingPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await deliver_github_all(session, github_client)
        await session.refresh(processed)

    assert processed is not None
    assert processed.status == "running"
    assert processed.stage == "retrying_agent_start"
    assert processed.agent_session_id is None
    assert processed.failure_code is None
    assert processed.lock_owner is None
    assert processed.locked_until is None
    assert processed.available_at is not None
    assert processed.validation_warnings_json == [
        {
            "code": "pi_agent_start_retry",
            "message": "pi-agent runtime request failed: connection refused",
            "retry": 1,
        }
    ]
    assert len(github_client.issue_comments) == 1
    assert "Review status: reviewing" in github_client.issue_comments[0].body


async def test_review_worker_retries_transient_pi_agent_start_request(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
        retry_initial_delay_seconds=1,
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
            pi_agent_client=InfrastructureFailingPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(FakeGitHubClient()),
        )

    assert processed is not None
    assert processed.status == "running"
    assert processed.stage == "retrying_agent_start"
    assert processed.failure_code is None
    assert processed.validation_warnings_json[0]["code"] == (
        "pi_agent_start_retry"
    )


async def test_review_worker_fails_after_pi_agent_start_retries_are_exhausted(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
        retry_max_attempts=2,
        retry_initial_delay_seconds=1,
    )
    github_client = FakeGitHubClient()
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
        review_run.validation_warnings_json = [
            {"code": "pi_agent_start_retry", "retry": 1},
            {"code": "pi_agent_start_retry", "retry": 2},
        ]
        session.add(review_run)
        await session.commit()

        processed = await process_next_review_run(
            session,
            settings=settings,
            pi_agent_client=InfrastructureFailingPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await deliver_github_all(session, github_client)
        await session.refresh(processed)

    assert processed is not None
    assert processed.status == "failed"
    assert processed.failure_code == "pi_agent_infrastructure_error"
    assert processed.error == "pi-agent runtime request failed: connection refused"
    assert len(github_client.issue_comments) == 1
    assert "Review status: failed" in github_client.issue_comments[0].body
    assert (
        "Failure category: pi_agent_infrastructure_error"
        in github_client.issue_comments[0].body
    )


async def test_review_worker_retries_transient_pi_agent_session_request(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
        worker_poll_interval_seconds=1,
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
        review_run.agent_session_id = "session-1"
        session.add(review_run)
        await session.commit()

        processed = await process_next_review_run(
            session,
            settings=settings,
            pi_agent_client=SessionRequestFailingPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(FakeGitHubClient()),
        )

    assert processed is not None
    assert processed.status == "running"
    assert processed.stage == "waiting_for_agent"
    assert processed.agent_session_id == "session-1"
    assert processed.failure_code is None
    assert processed.validation_warnings_json == [
        {
            "code": "pi_agent_session_retry",
            "message": "pi-agent runtime request failed: timeout",
        }
    ]


async def test_review_worker_reports_waiting_for_human(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
        worker_poll_interval_seconds=1,
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
            pi_agent_client=WaitingForHumanPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(FakeGitHubClient()),
        )

    assert processed is not None
    assert processed.status == "running"
    assert processed.stage == "waiting_for_human"
    assert processed.agent_status == "waiting_for_input"
    assert processed.agent_session_id == "session-1"


async def test_review_worker_keeps_agent_id_on_transient_status_failure(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
        worker_poll_interval_seconds=1,
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
        review_run.agent_session_id = "session-1"
        session.add(review_run)
        await session.commit()

        processed = await process_next_review_run(
            session,
            settings=settings,
            pi_agent_client=SessionRequestFailingPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(FakeGitHubClient()),
        )

    assert processed is not None
    assert processed.status == "running"
    assert processed.stage == "waiting_for_agent"
    assert processed.agent_session_id == "session-1"
    assert processed.failure_code is None
    assert processed.validation_warnings_json == [
        {
            "code": "pi_agent_session_retry",
            "message": "pi-agent runtime request failed: timeout",
        }
    ]


async def test_review_worker_publishes_failed_summary_on_invalid_result(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
    )
    github_client = FakeGitHubClient()
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
            pi_agent_client=InvalidResultPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await deliver_github_all(session, github_client)
        await session.refresh(processed)

    assert processed is not None
    assert processed.status == "failed"
    assert processed.failure_code == "invalid_result"
    assert "Review status: failed" in github_client.issue_comments[0].body
    assert "Failure category: invalid_result" in github_client.issue_comments[0].body


async def test_review_worker_marks_failed_on_unexpected_result_collection_error(
    session_factory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
    )
    github_client = FakeGitHubClient()

    async def fail_collect(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "review_orchestrator.worker.collect_review_result",
        fail_collect,
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
            pi_agent_client=ResultPiAgentClient(),
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await deliver_github_all(session, github_client)
        await session.refresh(processed)

    assert processed is not None
    assert processed.status == "failed"
    assert processed.failure_code == "worker_exception"
    assert processed.error == "database unavailable"
    assert "Review status: failed" in github_client.issue_comments[0].body
    assert "Failure category: worker_exception" in github_client.issue_comments[0].body


async def test_review_run_timeouts_publish_summary_and_cancel_pi_agent(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        review_run_soft_timeout_seconds=10,
        review_run_timeout_seconds=20,
    )
    github_client = FakeGitHubClient()
    pi_agent_client = FakePiAgentClient()
    async with session_factory() as session:
        soft_run = ReviewRun(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=41,
            base_sha="a" * 40,
            head_sha="b" * 40,
            status="running",
            started_at=utc_now() - timedelta(seconds=11),
        )
        hard_run = ReviewRun(
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            base_sha="a" * 40,
            head_sha="c" * 40,
            status="running",
            started_at=utc_now() - timedelta(seconds=21),
            agent_session_id="session-1",
        )
        session.add_all([soft_run, hard_run])
        await session.commit()

        touched = await process_review_run_timeouts(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            provider_registry=github_registry(github_client),
        )
        await deliver_github_all(session, github_client)
        for review_run in touched:
            await session.refresh(review_run)

    statuses = {run.pull_request_number: run.status for run in touched}
    assert statuses == {41: "running", 42: "failed"}
    assert pi_agent_client.cancelled_session_ids == ["session-1"]
    bodies = [comment.body for comment in github_client.issue_comments]
    assert any("Review status: delayed" in body for body in bodies)
    assert any("Failure category: hard_timeout" in body for body in bodies)


async def test_soft_timeout_summary_is_not_overwritten_by_same_worker_pass(
    session_factory,
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
        review_run_soft_timeout_seconds=10,
        review_run_timeout_seconds=60,
    )
    github_client = FakeGitHubClient()
    pi_agent_client = FakePiAgentClient()
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
        review_run = ReviewRun(
            pull_request_context_id=context.id,
            provider="github",
            repo_full_name="example/repo",
            pull_request_number=42,
            base_sha="a" * 40,
            head_sha="b" * 40,
            status="running",
            stage="waiting_for_result",
            workspace_path=str(tmp_path / "existing-workspace"),
            agent_session_id="session-1",
            started_at=utc_now() - timedelta(seconds=11),
        )
        session.add(review_run)
        await session.commit()

        await process_review_run_timeouts(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            provider_registry=github_registry(github_client),
        )
        processed = await process_next_review_run(
            session,
            settings=settings,
            pi_agent_client=pi_agent_client,
            worker_id="worker-1",
            provider_registry=github_registry(github_client),
        )
        await deliver_github_all(session, github_client)
        await session.refresh(processed)

    assert processed is not None
    assert processed.status == "running"
    assert len(github_client.issue_comments) == 1
    assert "Review status: delayed" in github_client.issue_comments[0].body
    assert "Review status: reviewing" not in github_client.issue_comments[0].body


def utc_future():
    from datetime import timedelta

    from review_orchestrator.models import utc_now

    return utc_now() + timedelta(minutes=5)
