from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select

from review_orchestrator.application.delivery import (
    claim_next_delivery,
    process_next_delivery,
)
from review_orchestrator.application.services import (
    ReviewRequest,
    enqueue_review_comment_status,
    handle_review_requested,
)
from review_orchestrator.application.worker import (
    process_next_review_run,
)
from review_orchestrator.config import Settings
from review_orchestrator.db import create_engine, create_session_factory, init_models
from review_orchestrator.github import GitHubAdapter, GitHubClientError
from review_orchestrator.models import (
    DeliveryOutbox,
    ProviderEventInbox,
    PullRequestContext,
    ReviewCommentRef,
    ReviewCommentSlot,
    ReviewRun,
    utc_now,
)
from review_orchestrator.pi_agent import PiAgentSession
from review_orchestrator.providers import ProviderRegistry


class Comment:
    def __init__(self, comment_id: str, pull_request_number: int, body: str) -> None:
        self.id = comment_id
        self.pull_request_number = pull_request_number
        self.body = body


class RecordingGitHubClient:
    def __init__(self, *, create_failures: int = 0) -> None:
        self.issue_comments: list[Comment] = []
        self.call_order: list[str] = []
        self.create_count = 0
        self.update_count = 0
        self.create_failures = create_failures

    async def list_issue_comments(self, repo_full_name, pull_request_number):
        del repo_full_name
        return [
            comment
            for comment in self.issue_comments
            if comment.pull_request_number == pull_request_number
        ]

    async def create_issue_comment(self, repo_full_name, pull_request_number, body):
        del repo_full_name
        self.create_count += 1
        if self.create_failures:
            self.create_failures -= 1
            raise GitHubClientError("provider temporarily unavailable")
        self.call_order.append("placeholder")
        comment = Comment(
            f"summary-{self.create_count}",
            pull_request_number,
            body,
        )
        self.issue_comments.append(comment)
        return comment.id

    async def update_issue_comment(self, repo_full_name, comment_id, body):
        del repo_full_name
        self.update_count += 1
        self.call_order.append("result")
        comment = next(item for item in self.issue_comments if item.id == comment_id)
        comment.body = body
        return comment.id

    async def list_pull_request_files(self, repo_full_name, pull_request_number):
        del repo_full_name, pull_request_number
        return []


class ImmediateResultPiAgentClient:
    def __init__(self, call_order: list[str]) -> None:
        self.call_order = call_order
        self.started = 0
        self.session = PiAgentSession(
            id="session-immediate",
            status="completed",
            stage="completed",
            provider="openai",
            model="gpt-5.4",
            thinking_level="high",
            result={"summary": "No issues found.", "findings": []},
        )

    async def start_session(self, review_input, **kwargs):
        del review_input, kwargs
        self.started += 1
        self.call_order.append("agent")
        return self.session

    async def get_session(self, session_id: str):
        assert session_id == self.session.id
        return self.session

    async def cancel_session(self, session_id: str):
        assert session_id == self.session.id
        return self.session


async def _factory(tmp_path: Path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/comment-gate.db",
        workspace_root=str(tmp_path / "workspaces"),
        git_cache_root=str(tmp_path / "git-cache"),
    )
    engine = create_engine(settings)
    await init_models(engine)
    return settings, engine, create_session_factory(engine)


def _registry(github_client) -> ProviderRegistry:
    return ProviderRegistry([GitHubAdapter(github_client)])


async def _create_review_request(
    session,
    *,
    delivery_id: str = "gate-delivery-1",
    pull_request_number: int = 42,
    head_sha: str = "b" * 40,
) -> ReviewRun:
    event = ProviderEventInbox(
        provider="github",
        delivery_id=delivery_id,
        provider_event="pull_request",
        provider_action="opened",
        internal_event="pr_opened",
        repo_full_name="example/repo",
        pull_request_number=pull_request_number,
        head_sha=head_sha,
        dedupe_key=f"github:{delivery_id}",
        payload_digest="digest",
        payload={},
        status="received",
    )
    context = PullRequestContext(
        provider="github",
        repo_full_name="example/repo",
        pull_request_number=pull_request_number,
        base_sha="a" * 40,
        head_sha=head_sha,
        status="open",
    )
    session.add_all([event, context])
    await session.flush()
    review_run, created = await handle_review_requested(
        session,
        ReviewRequest(
            reason="automatic",
            trigger_event_id=event.id,
            pull_request_context_id=context.id,
        ),
    )
    assert created is True
    event.review_run_id = review_run.id
    event.status = "queued"
    review_run.workspace_path = "/tmp/already-prepared-workspace"
    await session.commit()
    return review_run


async def _drain_deliveries(session, registry) -> None:
    for index in range(10):
        delivery = await process_next_delivery(
            session,
            worker_id=f"delivery-{index}",
            provider_registry=registry,
            retry_delay_seconds=0,
        )
        if delivery is None:
            return
    raise AssertionError("Delivery queue did not drain.")


async def test_placeholder_is_a_hard_gate_and_result_updates_the_same_slot(
    tmp_path: Path,
) -> None:
    settings, engine, factory = await _factory(tmp_path)
    github = RecordingGitHubClient()
    registry = _registry(github)
    pi_agent = ImmediateResultPiAgentClient(github.call_order)
    try:
        async with factory() as session:
            review_run = await _create_review_request(session)

            blocked = await process_next_review_run(
                session,
                settings=settings,
                pi_agent_client=pi_agent,
                worker_id="execution-before-placeholder",
                provider_registry=registry,
            )
            assert blocked is None
            assert pi_agent.started == 0

            placeholder = await process_next_delivery(
                session,
                worker_id="placeholder-worker",
                provider_registry=registry,
            )
            assert placeholder is not None
            assert placeholder.operation == "review_placeholder"
            await session.refresh(review_run)
            assert review_run.status == "queued"
            assert review_run.stage == "placeholder_ready"
            assert "Review status: reviewing" in github.issue_comments[0].body

            executed = await process_next_review_run(
                session,
                settings=settings,
                pi_agent_client=pi_agent,
                worker_id="execution-after-placeholder",
                provider_registry=registry,
            )
            assert executed is not None
            assert pi_agent.started == 1
            assert executed.execution_status == "completed"
            assert executed.status == "awaiting_delivery"
            assert "Review status: reviewing" in github.issue_comments[0].body

            await _drain_deliveries(session, registry)
            await session.refresh(executed)
            slot = (
                await session.execute(
                    select(ReviewCommentSlot).where(
                        ReviewCommentSlot.review_run_id == executed.id
                    )
                )
            ).scalar_one()

            assert github.call_order[:3] == ["placeholder", "agent", "result"]
            assert github.create_count == 1
            assert github.update_count == 1
            assert len(github.issue_comments) == 1
            assert slot.provider_comment_id == github.issue_comments[0].id
            assert slot.status == "finalized"
            assert slot.result_version == 1
            assert executed.status == "completed"
            assert "Review status: completed" in github.issue_comments[0].body
            assert slot.marker in github.issue_comments[0].body
            assert executed.id in github.issue_comments[0].body
    finally:
        await engine.dispose()


async def test_expired_placeholder_lease_recovers_by_exact_marker_without_duplicate(
    tmp_path: Path,
) -> None:
    _, engine, factory = await _factory(tmp_path)
    github = RecordingGitHubClient()
    registry = _registry(github)
    try:
        async with factory() as session:
            review_run = await _create_review_request(session)
            claimed = await claim_next_delivery(
                session,
                worker_id="crashed-worker",
                lock_seconds=1,
            )
            assert claimed is not None

            adapter = registry.get("github")
            assert adapter is not None
            ref = await adapter.publish_summary_comment(
                session,
                review_run,
                status_text="reviewing",
            )
            assert ref is not None
            assert github.create_count == 1

            # Model a crash after the Provider accepted create but before the
            # binding transaction became durable.
            await session.execute(
                delete(ReviewCommentRef).where(
                    ReviewCommentRef.review_run_id == review_run.id
                )
            )
            slot = (
                await session.execute(
                    select(ReviewCommentSlot).where(
                        ReviewCommentSlot.review_run_id == review_run.id
                    )
                )
            ).scalar_one()
            review_run.summary_comment_id = None
            slot.provider_comment_id = None
            session.add_all([review_run, slot])
            await session.commit()

            claimed.locked_until = utc_now() - timedelta(seconds=1)
            session.add(claimed)
            await session.commit()
            recovered = await process_next_delivery(
                session,
                worker_id="recovery-worker",
                provider_registry=registry,
                now=utc_now(),
            )
            await session.refresh(review_run)

            assert recovered is not None
            assert recovered.id == claimed.id
            assert recovered.attempt == 2
            assert recovered.status == "delivered"
            assert github.create_count == 1
            assert github.update_count == 1
            assert len(github.issue_comments) == 1
            assert review_run.status == "queued"
    finally:
        await engine.dispose()


async def test_core_placeholder_retries_without_terminal_attempt_limit(
    tmp_path: Path,
) -> None:
    _, engine, factory = await _factory(tmp_path)
    github = RecordingGitHubClient(create_failures=2)
    registry = _registry(github)
    try:
        async with factory() as session:
            review_run = await _create_review_request(session)
            for attempt in (1, 2):
                delivery = await process_next_delivery(
                    session,
                    worker_id=f"retry-{attempt}",
                    provider_registry=registry,
                    retry_delay_seconds=0,
                )
                assert delivery is not None
                assert delivery.status == "queued"
                assert delivery.attempt == attempt
                await session.refresh(review_run)
                assert review_run.status == "awaiting_delivery"
                assert review_run.execution_status == "pending"
                slot = (
                    await session.execute(
                        select(ReviewCommentSlot).where(
                            ReviewCommentSlot.review_run_id == review_run.id
                        )
                    )
                ).scalar_one()
                assert slot.status == "retry_wait"

            delivered = await process_next_delivery(
                session,
                worker_id="retry-3",
                provider_registry=registry,
                retry_delay_seconds=0,
            )
            await session.refresh(review_run)
            assert delivered is not None
            assert delivered.status == "delivered"
            assert delivered.attempt == 3
            assert delivered.max_attempts == 0
            assert review_run.status == "queued"
            assert review_run.stage == "placeholder_ready"
            assert github.create_count == 3
            assert len(github.issue_comments) == 1
    finally:
        await engine.dispose()


async def test_result_delivery_cannot_target_another_runs_comment_slot(
    tmp_path: Path,
) -> None:
    _, engine, factory = await _factory(tmp_path)
    github = RecordingGitHubClient()
    registry = _registry(github)
    try:
        async with factory() as session:
            first = await _create_review_request(session)
            second = await _create_review_request(
                session,
                delivery_id="gate-delivery-2",
                pull_request_number=43,
                head_sha="c" * 40,
            )
            for worker_id in ("placeholder-1", "placeholder-2"):
                delivered = await process_next_delivery(
                    session,
                    worker_id=worker_id,
                    provider_registry=registry,
                )
                assert delivered is not None
                assert delivered.operation == "review_placeholder"

            second_slot = (
                await session.execute(
                    select(ReviewCommentSlot).where(
                        ReviewCommentSlot.review_run_id == second.id
                    )
                )
            ).scalar_one()
            await enqueue_review_comment_status(
                session,
                first,
                status_text="completed",
            )
            result_delivery = (
                await session.execute(
                    select(DeliveryOutbox).where(
                        DeliveryOutbox.task_id == first.id,
                        DeliveryOutbox.operation == "review_result",
                    )
                )
            ).scalar_one()
            result_delivery.payload_json = {
                **result_delivery.payload_json,
                "slot_id": second_slot.id,
            }
            session.add(result_delivery)
            await session.commit()

            rejected = await process_next_delivery(
                session,
                worker_id="wrong-slot",
                provider_registry=registry,
                retry_delay_seconds=0,
            )
            await session.refresh(second_slot)

            assert rejected is not None
            assert rejected.status == "queued"
            assert rejected.attempt == 1
            assert "missing its comment slot" in (rejected.last_error or "")
            assert github.update_count == 0
            assert second_slot.status == "ready"
    finally:
        await engine.dispose()


async def test_stale_progress_cannot_overwrite_a_newer_terminal_result(
    tmp_path: Path,
) -> None:
    _, engine, factory = await _factory(tmp_path)
    github = RecordingGitHubClient()
    registry = _registry(github)
    try:
        async with factory() as session:
            review_run = await _create_review_request(session)
            placeholder = await process_next_delivery(
                session,
                worker_id="placeholder",
                provider_registry=registry,
            )
            assert placeholder is not None

            await enqueue_review_comment_status(
                session,
                review_run,
                status_text="soft_timeout",
            )
            await enqueue_review_comment_status(
                session,
                review_run,
                status_text="completed",
            )
            await _drain_deliveries(session, registry)
            await session.refresh(review_run)
            slot = (
                await session.execute(
                    select(ReviewCommentSlot).where(
                        ReviewCommentSlot.review_run_id == review_run.id
                    )
                )
            ).scalar_one()

            assert github.update_count == 1
            assert "Review status: completed" in github.issue_comments[0].body
            assert slot.result_version == 2
            assert slot.status == "finalized"
            assert review_run.status == "completed"
    finally:
        await engine.dispose()
