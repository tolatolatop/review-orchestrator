"""Background review and agent-task execution."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.application.services import (
    collect_review_result,
    create_review_run,
    get_or_create_review_config,
    start_review_session,
    sync_review_session,
)
from review_orchestrator.domain.models import (
    AgentTask,
    ProviderEventInbox,
    PullRequestContext,
    ReviewRun,
    utc_now,
)
from review_orchestrator.domain.review_results import ChangedFile
from review_orchestrator.domain.schemas import ReviewRunCreate, WorkspacePrepareRequest
from review_orchestrator.infrastructure.config import Settings
from review_orchestrator.infrastructure.workspaces import prepare_workspace
from review_orchestrator.integrations.github import (
    GitHubAdapter,
    GitHubClient,
)
from review_orchestrator.integrations.gitlab import (
    GitLabAdapter,
    GitLabClient,
)
from review_orchestrator.integrations.pi_agent import (
    AgentInstructionHistoryItem,
    AgentInstructionInput,
    AgentInstructionRepositoryContext,
    AgentTaskResult,
    PiAgentClient,
    PiAgentClientError,
)
from review_orchestrator.integrations.providers import (
    ProviderAdapter,
    ProviderError,
    ProviderRegistry,
)

WORKER_ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "superseded"}
TIMEOUT_EVENTS = {
    "soft": "review_run.soft_timeout",
    "hard": "review_run.hard_timeout",
}


def build_worker_provider_registry(
    *,
    github_client: GitHubClient | None = None,
    gitlab_client: GitLabClient | None = None,
) -> ProviderRegistry:
    adapters: list[ProviderAdapter] = []
    if github_client is not None:
        adapters.append(GitHubAdapter(github_client))
    if gitlab_client is not None:
        adapters.append(GitLabAdapter(gitlab_client))
    return ProviderRegistry(adapters)


async def acquire_next_review_run(
    session: AsyncSession,
    *,
    worker_id: str,
    lock_seconds: int = 300,
    now: datetime | None = None,
) -> ReviewRun | None:
    now = now or utc_now()
    result = await session.execute(
        select(ReviewRun)
        .where(
            or_(
                ReviewRun.status == "queued",
                ReviewRun.status == "running",
            ),
            or_(
                ReviewRun.locked_until.is_(None),
                ReviewRun.locked_until < now,
            ),
        )
        .order_by(ReviewRun.status == "running", ReviewRun.created_at)
        .limit(1)
    )
    review_run = result.scalar_one_or_none()
    if review_run is None:
        return None

    if review_run.status == "queued":
        review_run.status = "running"
        review_run.stage = "start"
    review_run.lock_owner = worker_id
    review_run.locked_until = now + timedelta(seconds=lock_seconds)
    review_run.started_at = review_run.started_at or now
    session.add(review_run)
    await session.commit()
    await session.refresh(review_run)
    return review_run


async def acquire_next_agent_task(
    session: AsyncSession,
    *,
    worker_id: str,
    lock_seconds: int = 300,
    now: datetime | None = None,
) -> AgentTask | None:
    now = now or utc_now()
    result = await session.execute(
        select(AgentTask)
        .where(
            or_(
                AgentTask.status == "queued",
                and_(
                    AgentTask.task_type == "message_command",
                    AgentTask.status == "running",
                ),
            ),
            or_(AgentTask.locked_until.is_(None), AgentTask.locked_until < now),
        )
        .order_by(AgentTask.created_at)
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return None
    if task.task_type != "message_command":
        task.status = "running"
        task.result_json = {"worker_id": worker_id}
    task.lock_owner = worker_id
    task.locked_until = now + timedelta(seconds=lock_seconds)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def process_next_agent_task(
    session: AsyncSession,
    *,
    worker_id: str,
    settings: Settings | None = None,
    pi_agent_client: PiAgentClient | None = None,
    github_client: GitHubClient | None = None,
    gitlab_client: GitLabClient | None = None,
    provider_registry: ProviderRegistry | None = None,
) -> AgentTask | None:
    task = await acquire_next_agent_task(
        session,
        worker_id=worker_id,
        lock_seconds=settings.worker_lock_seconds if settings else 300,
    )
    if task is None:
        return None
    registry = provider_registry or build_worker_provider_registry(
        github_client=github_client,
        gitlab_client=gitlab_client,
    )

    if task.task_type == "message_command":
        if settings is None or pi_agent_client is None:
            return await _fail_command_agent_task(
                session,
                task,
                failure_code="worker_not_configured",
                error=(
                    "Message-command execution requires worker settings and pi-agent."
                ),
                provider_registry=registry,
            )
        try:
            return await _process_command_agent_task(
                session,
                task,
                settings=settings,
                pi_agent_client=pi_agent_client,
                github_client=github_client,
                provider_registry=registry,
            )
        except Exception as exc:
            return await _fail_command_agent_task(
                session,
                task,
                failure_code="agent_task_failed",
                error=str(exc),
                provider_registry=registry,
            )
        finally:
            await _release_agent_task_lock(
                session,
                task.id,
                retry_after_seconds=settings.worker_poll_interval_seconds,
            )

    try:
        context = await _task_context(session, task)
        if context is None:
            try:
                context = await _hydrate_task_context(
                    session,
                    task,
                    provider_registry=registry,
                )
            except ProviderError as exc:
                return await _fail_agent_task(
                    session,
                    task,
                    failure_code="provider_context_lookup_failed",
                    error=str(exc),
                    provider_registry=registry,
                )
        if context is None:
            return await _fail_agent_task(
                session,
                task,
                failure_code="missing_pr_context",
                error="Pull request context was not found for mention task.",
                provider_registry=registry,
            )

        review_run = await create_review_run(
            session,
            ReviewRunCreate(
                provider=context.provider,
                repo_full_name=context.repo_full_name,
                pull_request_number=context.pull_request_number,
                base_sha=context.base_sha,
                head_sha=context.head_sha,
                force=True,
            ),
            trigger_type="mention",
            trigger_event_id=task.provider_event_id,
        )
        task.status = "completed"
        task.result_json = {"review_run_id": review_run.id, "status": review_run.status}
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task
    except Exception as exc:
        return await _fail_agent_task(
            session,
            task,
            failure_code="agent_task_failed",
            error=str(exc),
            provider_registry=registry,
        )


async def _process_command_agent_task(
    session: AsyncSession,
    task: AgentTask,
    *,
    settings: Settings,
    pi_agent_client: PiAgentClient,
    github_client: GitHubClient | None,
    provider_registry: ProviderRegistry,
) -> AgentTask:
    if task.response_comment_id is None or task.stage == "placeholder_pending":
        try:
            await _publish_agent_task_comment(
                session, task, provider_registry=provider_registry, state="working"
            )
        except ProviderError as exc:
            task.last_publish_error = str(exc)
            task.error_message = str(exc)
            session.add(task)
            await session.commit()
            await session.refresh(task)
            return task

    if task.stage == "cancellation_pending":
        return await _cancel_command_agent_task(
            session,
            task,
            pi_agent_client=pi_agent_client,
            provider_registry=provider_registry,
        )
    if task.stage == "publishing_failure":
        return await _fail_command_agent_task(
            session,
            task,
            failure_code=task.failure_code or "agent_task_failed",
            error=task.error_message or "Agent task failed.",
            provider_registry=provider_registry,
        )

    if not (task.command_text or "").strip():
        task.result_text = "Please include a request after the bot mention."
        task.result_json = {
            "outcome": "needs_clarification",
            "answer": task.result_text,
            "references": [],
        }
        return await _complete_command_agent_task(
            session, task, provider_registry=provider_registry
        )

    older_result = await session.execute(
        select(AgentTask.id)
        .where(
            AgentTask.provider == task.provider,
            AgentTask.repo_full_name == task.repo_full_name,
            AgentTask.pull_request_number == task.pull_request_number,
            AgentTask.task_type == "message_command",
            AgentTask.status.in_({"queued", "running"}),
            AgentTask.created_at < task.created_at,
        )
        .limit(1)
    )
    if older_result.scalar_one_or_none() is not None:
        task.status = "queued"
        task.stage = "waiting_for_turn"
        session.add(task)
        await session.commit()
        await session.refresh(task)
        try:
            await _publish_agent_task_comment(
                session,
                task,
                provider_registry=provider_registry,
                state="queued",
            )
        except ProviderError as exc:
            task.last_publish_error = str(exc)
            session.add(task)
            await session.commit()
            await session.refresh(task)
        return task

    context = await _task_context(session, task)
    if context is None:
        try:
            context = await _hydrate_task_context(
                session, task, provider_registry=provider_registry
            )
        except ProviderError as exc:
            return await _fail_command_agent_task(
                session,
                task,
                failure_code="provider_context_lookup_failed",
                error=str(exc),
                provider_registry=provider_registry,
            )
    if context is None:
        return await _fail_command_agent_task(
            session,
            task,
            failure_code="missing_pr_context",
            error="Pull request context was not found for message command.",
            provider_registry=provider_registry,
        )
    if context.status not in {"open", "opened"}:
        return await _fail_command_agent_task(
            session,
            task,
            failure_code="pull_request_closed",
            error="The pull request is no longer open.",
            provider_registry=provider_registry,
        )
    command_config = await get_or_create_review_config(
        session,
        provider=task.provider,
        repo_full_name=task.repo_full_name,
    )
    if command_config.agent_commands_enabled is False:
        return await _fail_command_agent_task(
            session,
            task,
            failure_code="agent_commands_disabled",
            error="Agent commands are disabled for this repository.",
            provider_registry=provider_registry,
        )

    task.head_sha = task.head_sha or context.head_sha
    if task.workspace_path is None:
        task.status = "running"
        task.stage = "preparing_workspace"
        session.add(task)
        await session.commit()
        try:
            workspace = await prepare_workspace(
                session,
                settings,
                _workspace_request_from_context(context),
                github_client=github_client if task.provider == "github" else None,
            )
        except Exception as exc:
            return await _fail_command_agent_task(
                session,
                task,
                failure_code="workspace_failed",
                error=str(exc),
                provider_registry=provider_registry,
            )
        if workspace.status == "failed":
            return await _fail_command_agent_task(
                session,
                task,
                failure_code=workspace.failure_code or "workspace_failed",
                error=workspace.failure_message or "Workspace preparation failed.",
                provider_registry=provider_registry,
            )
        task.workspace_path = workspace.workspace_path
        session.add(task)
        await session.commit()
        await session.refresh(task)

    if task.agent_session_id is None:
        task.status = "running"
        task.stage = "starting_agent"
        task.started_at = task.started_at or utc_now()
        task.deadline_at = task.started_at + timedelta(
            seconds=settings.agent_task_timeout_seconds
        )
        session.add(task)
        await session.commit()
        instruction = await _build_agent_instruction(session, task, context, settings)
        task.agent_start_attempts += 1
        session.add(task)
        await session.commit()
        try:
            runtime_session = await pi_agent_client.start_instruction_session(
                instruction,
                skill=(
                    command_config.default_agent_command_skill
                    or settings.agent_command_skill
                ),
                profile=(
                    command_config.default_agent_command_profile
                    or settings.agent_command_profile
                ),
                provider=settings.pi_agent_provider,
                model=settings.pi_agent_model,
                thinking_level=settings.pi_agent_thinking_level,
                model_base_url=settings.pi_agent_model_base_url,
            )
        except PiAgentClientError as exc:
            if (
                exc.infrastructure_failure
                and task.agent_start_attempts < settings.retry_max_attempts
            ):
                task.status = "queued"
                task.stage = "retrying_agent_start"
                task.error_message = str(exc)
                task.locked_until = utc_now() + timedelta(
                    seconds=settings.retry_initial_delay_seconds
                )
                session.add(task)
                await session.commit()
                await session.refresh(task)
                return task
            return await _fail_command_agent_task(
                session,
                task,
                failure_code="agent_start_failed",
                error=str(exc),
                provider_registry=provider_registry,
            )
        task.agent_session_id = runtime_session.id
        task.error_message = None
    else:
        try:
            runtime_session = await pi_agent_client.get_session(task.agent_session_id)
        except PiAgentClientError as exc:
            return await _fail_command_agent_task(
                session,
                task,
                failure_code="agent_sync_failed",
                error=str(exc),
                provider_registry=provider_registry,
            )

    task.agent_status = runtime_session.status.value
    task.agent_provider = runtime_session.provider
    task.agent_model = runtime_session.model
    task.agent_thinking_level = runtime_session.thinking_level
    if runtime_session.status == "completed":
        try:
            result = AgentTaskResult.model_validate(runtime_session.result)
            await _validate_task_result_references(task, result)
        except Exception:
            return await _fail_command_agent_task(
                session,
                task,
                failure_code="invalid_result",
                error="Agent returned an invalid structured task result.",
                provider_registry=provider_registry,
            )
        task.result_text = result.answer
        task.result_json = result.model_dump()
        return await _complete_command_agent_task(
            session, task, provider_registry=provider_registry
        )
    if runtime_session.status in {"failed", "cancelled"}:
        return await _fail_command_agent_task(
            session,
            task,
            failure_code=(
                "agent_cancelled"
                if runtime_session.status == "cancelled"
                else "agent_failed"
            ),
            error=(
                runtime_session.error
                or f"Agent session {runtime_session.status.value}."
            ),
            provider_registry=provider_registry,
        )

    task.status = "running"
    task.stage = "waiting_for_agent"
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _build_agent_instruction(
    session: AsyncSession,
    task: AgentTask,
    context: PullRequestContext,
    settings: Settings,
) -> AgentInstructionInput:
    result = await session.execute(
        select(AgentTask)
        .where(
            AgentTask.provider == task.provider,
            AgentTask.repo_full_name == task.repo_full_name,
            AgentTask.pull_request_number == task.pull_request_number,
            AgentTask.task_type == "message_command",
            AgentTask.status == "completed",
            AgentTask.created_at < task.created_at,
            AgentTask.result_text.is_not(None),
        )
        .order_by(AgentTask.created_at.desc())
        .limit(settings.agent_task_max_history_turns)
    )
    history: list[AgentInstructionHistoryItem] = []
    remaining = settings.agent_task_max_history_chars
    for previous in reversed(list(result.scalars().all())):
        command = previous.command_text or ""
        answer = previous.result_text or ""
        size = len(command) + len(answer)
        if size > remaining:
            continue
        remaining -= size
        raw_outcome = (previous.result_json or {}).get("outcome", "answered")
        outcome = (
            raw_outcome
            if raw_outcome in {"answered", "needs_clarification", "refused"}
            else "answered"
        )
        history.append(
            AgentInstructionHistoryItem(
                author_login=previous.source_author_login or "user",
                command=command,
                answer=answer,
                outcome=outcome,
                head_sha=previous.head_sha or context.head_sha,
            )
        )
    return AgentInstructionInput(
        idempotency_key=f"agent-task:{task.id}:attempt:{task.attempt}",
        workspace_path=task.workspace_path or "",
        repository_context=AgentInstructionRepositoryContext(
            provider=task.provider,
            repo_full_name=task.repo_full_name,
            pr_number=task.pull_request_number,
            base_sha=context.base_sha,
            head_sha=task.head_sha or context.head_sha,
        ),
        text=task.command_text or "",
        author_login=task.source_author_login or "user",
        source_url=task.source_url,
        history=history,
    )


async def _validate_task_result_references(
    task: AgentTask,
    result: AgentTaskResult,
) -> None:
    if not task.workspace_path:
        raise ValueError("Task workspace is missing.")
    await asyncio.to_thread(_validate_task_result_reference_paths, task, result)


def _validate_task_result_reference_paths(
    task: AgentTask,
    result: AgentTaskResult,
) -> None:
    assert task.workspace_path is not None
    workspace = Path(task.workspace_path).resolve()
    for reference in result.references:
        target = (workspace / reference.path).resolve()
        try:
            target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("Task result reference escapes the workspace.") from exc
        if not target.is_file():
            raise ValueError(f"Task result reference does not exist: {reference.path}")


async def _complete_command_agent_task(
    session: AsyncSession,
    task: AgentTask,
    *,
    provider_registry: ProviderRegistry,
) -> AgentTask:
    task.status = "running"
    task.stage = "publishing_result"
    session.add(task)
    await session.commit()
    await session.refresh(task)
    try:
        await _publish_agent_task_comment(
            session, task, provider_registry=provider_registry, state="completed"
        )
    except ProviderError as exc:
        task.last_publish_error = str(exc)
        task.error_message = str(exc)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task
    task.status = "completed"
    task.stage = "completed"
    task.completed_at = utc_now()
    task.error_message = None
    task.failure_code = None
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _publish_agent_task_comment(
    session: AsyncSession,
    task: AgentTask,
    *,
    provider_registry: ProviderRegistry,
    state: str,
) -> str:
    adapter = provider_registry.get(task.provider)
    if adapter is None or not hasattr(adapter, "publish_agent_task_comment"):
        raise ProviderError(
            f"Provider {task.provider} cannot publish agent task comments.",
            provider=task.provider,
            operation="publish_agent_task_comment",
        )
    try:
        return await adapter.publish_agent_task_comment(session, task, state=state)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(
            str(exc),
            provider=task.provider,
            operation="publish_agent_task_comment",
        ) from exc


async def _fail_command_agent_task(
    session: AsyncSession,
    task: AgentTask,
    *,
    failure_code: str,
    error: str,
    provider_registry: ProviderRegistry,
) -> AgentTask:
    task.status = "running"
    task.stage = "publishing_failure"
    task.failure_code = failure_code
    task.error_message = error
    task.completed_at = utc_now()
    session.add(task)
    await session.commit()
    await session.refresh(task)
    try:
        await _publish_agent_task_comment(
            session, task, provider_registry=provider_registry, state="failed"
        )
    except ProviderError as exc:
        task.last_publish_error = str(exc)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task
    task.status = "failed"
    task.stage = "failed"
    task.last_publish_error = None
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _cancel_command_agent_task(
    session: AsyncSession,
    task: AgentTask,
    *,
    pi_agent_client: PiAgentClient,
    provider_registry: ProviderRegistry,
) -> AgentTask:
    if task.agent_session_id:
        try:
            await pi_agent_client.cancel_session(task.agent_session_id)
        except PiAgentClientError:
            pass
    task.status = "running"
    task.stage = "cancellation_pending"
    session.add(task)
    await session.commit()
    try:
        await _publish_agent_task_comment(
            session,
            task,
            provider_registry=provider_registry,
            state="cancelled",
        )
    except ProviderError as exc:
        task.last_publish_error = str(exc)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task
    task.status = "cancelled"
    task.stage = "cancelled"
    task.completed_at = utc_now()
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _release_agent_task_lock(
    session: AsyncSession,
    task_id: str,
    *,
    retry_after_seconds: float = 0,
) -> AgentTask | None:
    task = await session.get(AgentTask, task_id)
    if task is None:
        return None
    task.lock_owner = None
    if task.stage == "retrying_agent_start" and task.locked_until is not None:
        pass
    elif task.status in {"queued", "running"} and task.stage in {
        "waiting_for_turn",
        "waiting_for_agent",
        "publishing_result",
        "publishing_failure",
        "cancellation_pending",
        "placeholder_pending",
    }:
        task.locked_until = utc_now() + timedelta(seconds=retry_after_seconds)
    else:
        task.locked_until = None
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _fail_agent_task(
    session: AsyncSession,
    task: AgentTask,
    *,
    failure_code: str,
    error: str,
    provider_registry: ProviderRegistry,
) -> AgentTask:
    task.status = "failed"
    task.error_message = error
    session.add(task)
    await session.commit()
    await session.refresh(task)

    try:
        review_run = await _review_run_for_failed_agent_task(
            session,
            task,
            failure_code=failure_code,
            error=error,
        )
        await publish_review_run_status_comment(
            session,
            review_run,
            provider_registry=provider_registry,
            status_text="failed",
        )
    except Exception as exc:
        task.result_json = {
            **(task.result_json or {}),
            "summary_publish_error": str(exc),
        }
        session.add(task)
        await session.commit()
        await session.refresh(task)
    return task


async def _review_run_for_failed_agent_task(
    session: AsyncSession,
    task: AgentTask,
    *,
    failure_code: str,
    error: str,
) -> ReviewRun:
    context = await _task_context(session, task)
    review_run = await create_review_run(
        session,
        ReviewRunCreate(
            provider=task.provider,
            repo_full_name=task.repo_full_name,
            pull_request_number=task.pull_request_number,
            base_sha=context.base_sha if context else None,
            head_sha=(context.head_sha if context else _task_head_sha(task)),
            force=True,
        ),
        trigger_type="mention",
        trigger_event_id=task.provider_event_id,
    )
    review_run.status = "failed"
    review_run.failure_code = failure_code
    review_run.error = error
    review_run.completed_at = utc_now()
    session.add(review_run)
    await session.commit()
    await session.refresh(review_run)
    return review_run


def _task_head_sha(task: AgentTask) -> str:
    payload = (task.input_json or {}).get("payload")
    if isinstance(payload, dict):
        head_sha = _head_sha_from_payload(payload)
        if head_sha:
            return head_sha
    return "unknown"


def _head_sha_from_payload(payload: dict[str, Any]) -> str | None:
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        head = pull_request.get("head")
        if isinstance(head, dict):
            return _str_or_none(head.get("sha"))
    merge_request = payload.get("merge_request") or payload.get("object_attributes")
    if isinstance(merge_request, dict):
        last_commit = merge_request.get("last_commit")
        last_commit_sha = (
            _str_or_none(last_commit.get("id"))
            if isinstance(last_commit, dict)
            else None
        )
        return _str_or_none(merge_request.get("sha")) or last_commit_sha
    return None


async def process_next_review_run(
    session: AsyncSession,
    *,
    settings: Settings,
    pi_agent_client: PiAgentClient,
    worker_id: str,
    github_client: GitHubClient | None = None,
    gitlab_client: GitLabClient | None = None,
    provider_registry: ProviderRegistry | None = None,
) -> ReviewRun | None:
    review_run = await acquire_next_review_run(
        session,
        worker_id=worker_id,
        lock_seconds=settings.worker_lock_seconds,
    )
    if review_run is None:
        return None
    registry = provider_registry or build_worker_provider_registry(
        github_client=github_client,
        gitlab_client=gitlab_client,
    )

    release_lock = True
    try:
        if _should_publish_reviewing(review_run):
            await publish_review_run_status_comment(
                session,
                review_run,
                github_client=github_client,
                gitlab_client=gitlab_client,
                provider_registry=registry,
                status_text="reviewing",
            )
        context = await _review_run_context(session, review_run)
        if context is None:
            review_run.status = "failed"
            review_run.failure_code = "missing_pr_context"
            review_run.error = (
                "Pull request context is required for automatic execution."
            )
            session.add(review_run)
            await session.commit()
            await session.refresh(review_run)
            await publish_review_run_status_comment(
                session,
                review_run,
                github_client=github_client,
                gitlab_client=gitlab_client,
                provider_registry=registry,
                status_text="failed",
            )
            return review_run

        if not review_run.workspace_path:
            try:
                workspace = await prepare_workspace(
                    session,
                    settings,
                    _workspace_request_from_context(context),
                    github_client=(
                        github_client if review_run.provider == "github" else None
                    ),
                )
            except Exception as exc:
                review_run.status = "failed"
                review_run.failure_code = "workspace_failed"
                review_run.error = str(exc)
                session.add(review_run)
                await session.commit()
                await session.refresh(review_run)
                await publish_review_run_status_comment(
                    session,
                    review_run,
                    github_client=github_client,
                    gitlab_client=gitlab_client,
                    provider_registry=registry,
                    status_text="failed",
                )
                return review_run
            if workspace.status == "failed":
                review_run.status = "failed"
                review_run.failure_code = workspace.failure_code or "workspace_failed"
                review_run.error = workspace.failure_message
                session.add(review_run)
                await session.commit()
                await session.refresh(review_run)
                await publish_review_run_status_comment(
                    session,
                    review_run,
                    github_client=github_client,
                    gitlab_client=gitlab_client,
                    provider_registry=registry,
                    status_text="failed",
                )
                return review_run
            review_run.workspace_path = workspace.workspace_path
            session.add(review_run)
            await session.commit()
            await session.refresh(review_run)

        if not review_run.agent_session_id:
            review_run = await start_review_session(
                session,
                review_run,
                pi_agent_client=pi_agent_client,
                settings=settings,
                workspace_path=review_run.workspace_path,
            )
            if review_run.status == "failed":
                if await _schedule_pi_agent_retry(
                    session,
                    review_run,
                    settings=settings,
                ):
                    release_lock = False
                    return review_run
                await publish_review_run_status_comment(
                    session,
                    review_run,
                    github_client=github_client,
                    gitlab_client=gitlab_client,
                    provider_registry=registry,
                    status_text="failed",
                )
                return review_run
        review_run = await sync_review_session(
            session,
            review_run,
            pi_agent_client=pi_agent_client,
        )
        if review_run.status == "failed":
            if await _schedule_pi_agent_retry(
                session,
                review_run,
                settings=settings,
            ):
                release_lock = False
                return review_run
            await publish_review_run_status_comment(
                session,
                review_run,
                github_client=github_client,
                gitlab_client=gitlab_client,
                provider_registry=registry,
                status_text="failed",
            )
            return review_run
        if review_run.status == "cancelled":
            return review_run
        raw_result = (
            review_run.result_raw_json
            if review_run.agent_status == "completed"
            else None
        )
        if raw_result is None:
            if review_run.stage != "waiting_for_human":
                review_run.stage = review_run.stage or "waiting_for_agent"
            review_run.lock_owner = None
            review_run.locked_until = utc_now() + timedelta(
                seconds=settings.worker_poll_interval_seconds
            )
            session.add(review_run)
            await session.commit()
            await session.refresh(review_run)
            release_lock = False
            return review_run

        changed_files = await _fetch_changed_files(
            session,
            review_run,
            provider_registry=registry,
        )
        try:
            await collect_review_result(
                session,
                review_run,
                raw_output=raw_result,
                changed_files=changed_files,
            )
        except Exception as exc:
            await session.refresh(review_run)
            if review_run.status != "failed":
                review_run.status = "failed"
                review_run.failure_code = "worker_exception"
                review_run.error = str(exc)
                review_run.completed_at = utc_now()
                session.add(review_run)
                await session.commit()
                await session.refresh(review_run)
            await publish_review_run_status_comment(
                session,
                review_run,
                github_client=github_client,
                gitlab_client=gitlab_client,
                provider_registry=registry,
                status_text="failed",
            )
            return review_run
        config = await get_or_create_review_config(
            session,
            provider=review_run.provider,
            repo_full_name=review_run.repo_full_name,
        )
        adapter = registry.get(review_run.provider)
        if adapter is not None:
            await adapter.publish_summary_comment(
                session,
                review_run,
                status_text="completed",
                finding_stats=review_run.finding_count_by_severity,
            )
            if config.line_comments_enabled:
                await adapter.publish_line_comments(
                    session,
                    review_run,
                    changed_files=changed_files,
                )
        return await session.get(ReviewRun, review_run.id)
    except Exception as exc:
        review_run.status = "failed"
        review_run.failure_code = review_run.failure_code or "worker_exception"
        review_run.error = review_run.error or str(exc)
        review_run.completed_at = utc_now()
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)
        await publish_review_run_status_comment(
            session,
            review_run,
            github_client=github_client,
            gitlab_client=gitlab_client,
            provider_registry=registry,
            status_text="failed",
        )
        return review_run
    finally:
        if release_lock:
            await release_review_run_lock(session, review_run.id)


def _should_publish_reviewing(review_run: ReviewRun) -> bool:
    return (
        review_run.summary_comment_id is None
        and review_run.soft_timeout_emitted_at is None
        and review_run.stage in {None, "start"}
    )


async def process_agent_task_timeouts(
    session: AsyncSession,
    *,
    settings: Settings,
    pi_agent_client: PiAgentClient,
    github_client: GitHubClient | None = None,
    gitlab_client: GitLabClient | None = None,
    provider_registry: ProviderRegistry | None = None,
    now: datetime | None = None,
) -> list[AgentTask]:
    now = now or utc_now()
    registry = provider_registry or build_worker_provider_registry(
        github_client=github_client,
        gitlab_client=gitlab_client,
    )
    result = await session.execute(
        select(AgentTask).where(
            AgentTask.task_type == "message_command",
            AgentTask.status.in_({"queued", "running"}),
            AgentTask.stage.in_(
                {"starting_agent", "waiting_for_agent", "retrying_agent_start"}
            ),
            AgentTask.started_at.is_not(None),
        )
    )
    touched: list[AgentTask] = []
    for task in result.scalars().all():
        if task.started_at is None:
            continue
        started_at = task.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=now.tzinfo)
        elapsed = (now - started_at).total_seconds()
        if elapsed >= settings.agent_task_timeout_seconds:
            if task.hard_timeout_emitted_at is not None:
                continue
            task.hard_timeout_emitted_at = now
            if task.agent_session_id:
                try:
                    await pi_agent_client.cancel_session(task.agent_session_id)
                except PiAgentClientError:
                    pass
            failed = await _fail_command_agent_task(
                session,
                task,
                failure_code="hard_timeout",
                error="Agent task exceeded the hard timeout.",
                provider_registry=registry,
            )
            touched.append(failed)
            continue
        if (
            elapsed >= settings.agent_task_soft_timeout_seconds
            and task.soft_timeout_emitted_at is None
        ):
            task.soft_timeout_emitted_at = now
            session.add(task)
            await session.commit()
            await session.refresh(task)
            try:
                await _publish_agent_task_comment(
                    session,
                    task,
                    provider_registry=registry,
                    state="soft_timeout",
                )
            except ProviderError as exc:
                task.last_publish_error = str(exc)
                session.add(task)
                await session.commit()
                await session.refresh(task)
            touched.append(task)
    return touched


async def process_review_run_timeouts(
    session: AsyncSession,
    *,
    settings: Settings,
    pi_agent_client: PiAgentClient,
    github_client: GitHubClient | None = None,
    gitlab_client: GitLabClient | None = None,
    provider_registry: ProviderRegistry | None = None,
    now: datetime | None = None,
) -> list[ReviewRun]:
    now = now or utc_now()
    registry = provider_registry or build_worker_provider_registry(
        github_client=github_client,
        gitlab_client=gitlab_client,
    )
    result = await session.execute(
        select(ReviewRun).where(
            ReviewRun.status.in_(WORKER_ACTIVE_STATUSES),
        )
    )
    touched: list[ReviewRun] = []
    for review_run in result.scalars().all():
        started_at = review_run.started_at or review_run.created_at
        elapsed = (now - started_at).total_seconds()
        if (
            elapsed >= settings.review_run_timeout_seconds
            and review_run.hard_timeout_emitted_at is None
        ):
            await emit_timeout_event(
                session,
                review_run.id,
                timeout_kind="hard",
                now=now,
            )
            refreshed = await session.get(ReviewRun, review_run.id)
            if refreshed is None:
                continue
            await _cancel_pi_agent_after_hard_timeout(
                session,
                refreshed,
                pi_agent_client=pi_agent_client,
            )
            await publish_review_run_status_comment(
                session,
                refreshed,
                github_client=github_client,
                gitlab_client=gitlab_client,
                provider_registry=registry,
                status_text="failed",
            )
            touched.append(refreshed)
            continue

        if (
            elapsed >= settings.review_run_soft_timeout_seconds
            and review_run.soft_timeout_emitted_at is None
        ):
            await emit_timeout_event(
                session,
                review_run.id,
                timeout_kind="soft",
                now=now,
            )
            refreshed = await session.get(ReviewRun, review_run.id)
            if refreshed is None:
                continue
            await publish_review_run_status_comment(
                session,
                refreshed,
                github_client=github_client,
                gitlab_client=gitlab_client,
                provider_registry=registry,
                status_text="delayed",
            )
            touched.append(refreshed)
    return touched


async def publish_review_run_status_comment(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    github_client: GitHubClient | None = None,
    gitlab_client: GitLabClient | None = None,
    provider_registry: ProviderRegistry | None = None,
    status_text: str,
) -> None:
    original_failure_code = review_run.failure_code
    original_error = review_run.error
    registry = provider_registry or build_worker_provider_registry(
        github_client=github_client,
        gitlab_client=gitlab_client,
    )
    adapter = registry.get(review_run.provider)
    if adapter is None:
        return
    await adapter.publish_summary_comment(
        session,
        review_run,
        status_text=status_text,
        finding_stats=review_run.finding_count_by_severity,
    )

    await session.refresh(review_run)
    if original_failure_code and review_run.failure_code != original_failure_code:
        warnings = list(review_run.validation_warnings_json or [])
        warnings.append(
            {
                "code": "summary_publish_failed",
                "message": review_run.error,
            }
        )
        review_run.failure_code = original_failure_code
        review_run.error = original_error
        review_run.validation_warnings_json = warnings
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)


async def _cancel_pi_agent_after_hard_timeout(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    pi_agent_client: PiAgentClient,
) -> None:
    if not review_run.agent_session_id:
        return
    try:
        await pi_agent_client.cancel_session(review_run.agent_session_id)
    except PiAgentClientError as exc:
        review_run.error = (
            f"{review_run.error or 'Review run exceeded hard timeout.'} "
            f"pi-agent cleanup failed: {exc}"
        )
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)


async def mark_review_run_stage(
    session: AsyncSession,
    review_run_id: str,
    stage: str,
) -> ReviewRun | None:
    review_run = await session.get(ReviewRun, review_run_id)
    if review_run is None:
        return None
    if review_run.status not in TERMINAL_STATUSES:
        review_run.stage = stage
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)
    return review_run


async def release_review_run_lock(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewRun | None:
    review_run = await session.get(ReviewRun, review_run_id)
    if review_run is None:
        return None
    review_run.lock_owner = None
    review_run.locked_until = None
    session.add(review_run)
    await session.commit()
    await session.refresh(review_run)
    return review_run


async def emit_timeout_event(
    session: AsyncSession,
    review_run_id: str,
    *,
    timeout_kind: Literal["soft", "hard"],
    now: datetime | None = None,
) -> ProviderEventInbox | None:
    review_run = await session.get(ReviewRun, review_run_id)
    if review_run is None:
        return None
    if review_run.status in TERMINAL_STATUSES:
        return None

    now = now or utc_now()
    internal_event = TIMEOUT_EVENTS[timeout_kind]
    marker_attr = (
        "soft_timeout_emitted_at"
        if timeout_kind == "soft"
        else "hard_timeout_emitted_at"
    )
    if getattr(review_run, marker_attr) is not None:
        return await _get_internal_event(
            session,
            review_run_id=review_run.id,
            internal_event=internal_event,
        )

    event = ProviderEventInbox(
        provider="internal",
        delivery_id=f"{review_run.id}:{internal_event}",
        provider_event="review_run",
        provider_action=timeout_kind,
        internal_event=internal_event,
        repo_full_name=review_run.repo_full_name,
        pull_request_number=review_run.pull_request_number,
        head_sha=review_run.head_sha,
        dedupe_key=f"internal:{review_run.id}:{internal_event}",
        coalesce_key=(
            f"internal:{review_run.provider}:{review_run.repo_full_name}:"
            f"{review_run.pull_request_number}:timeout"
        ),
        payload_digest=_digest(review_run.id, internal_event),
        payload={"review_run_id": review_run.id, "timeout_kind": timeout_kind},
        status="received",
        review_run_id=review_run.id,
        processed_at=now,
    )
    setattr(review_run, marker_attr, now)
    review_run.stage = internal_event
    if timeout_kind == "hard":
        review_run.status = "failed"
        review_run.failure_code = "hard_timeout"
        review_run.error = "Review run exceeded hard timeout."
        review_run.completed_at = now
        review_run.lock_owner = None
        review_run.locked_until = None

    session.add(event)
    session.add(review_run)
    await session.commit()
    await session.refresh(event)
    return event


async def _get_internal_event(
    session: AsyncSession,
    *,
    review_run_id: str,
    internal_event: str,
) -> ProviderEventInbox | None:
    result = await session.execute(
        select(ProviderEventInbox).where(
            ProviderEventInbox.provider == "internal",
            ProviderEventInbox.review_run_id == review_run_id,
            ProviderEventInbox.internal_event == internal_event,
        )
    )
    return result.scalar_one_or_none()


def _digest(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


async def _task_context(
    session: AsyncSession,
    task: AgentTask,
) -> PullRequestContext | None:
    if task.pull_request_context_id:
        return await session.get(PullRequestContext, task.pull_request_context_id)
    result = await session.execute(
        select(PullRequestContext).where(
            PullRequestContext.provider == task.provider,
            PullRequestContext.repo_full_name == task.repo_full_name,
            PullRequestContext.pull_request_number == task.pull_request_number,
        )
    )
    return result.scalar_one_or_none()


async def _review_run_context(
    session: AsyncSession,
    review_run: ReviewRun,
) -> PullRequestContext | None:
    if review_run.pull_request_context_id:
        return await session.get(PullRequestContext, review_run.pull_request_context_id)
    result = await session.execute(
        select(PullRequestContext).where(
            PullRequestContext.provider == review_run.provider,
            PullRequestContext.repo_full_name == review_run.repo_full_name,
            PullRequestContext.pull_request_number == review_run.pull_request_number,
        )
    )
    return result.scalar_one_or_none()


def _workspace_request_from_context(
    context: PullRequestContext,
) -> WorkspacePrepareRequest:
    return WorkspacePrepareRequest.model_validate(
        {
            "provider": context.provider,
            "repository": {
                "full_name": context.repo_full_name,
                "clone_url": _clone_url(context),
            },
            "pull_request": {
                "number": context.pull_request_number,
                "base_sha": context.base_sha,
                "head_sha": context.head_sha,
                "is_fork": context.is_fork,
            },
        }
    )


def _clone_url(context: PullRequestContext) -> str:
    if context.provider == "github":
        return f"https://github.com/{context.repo_full_name}.git"
    if context.provider == "gitlab":
        return f"https://gitlab.com/{context.repo_full_name}.git"
    return context.html_url or context.repo_full_name


async def _hydrate_task_context(
    session: AsyncSession,
    task: AgentTask,
    *,
    provider_registry: ProviderRegistry,
) -> PullRequestContext | None:
    adapter = provider_registry.get(task.provider)
    if adapter is None:
        return None
    context = await adapter.get_pull_request_context(task)
    if context is None:
        return None

    session.add(context)
    await session.commit()
    await session.refresh(context)
    task.pull_request_context_id = context.id
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return context


async def _fetch_changed_files(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    provider_registry: ProviderRegistry,
) -> list[ChangedFile]:
    try:
        adapter = provider_registry.get(review_run.provider)
        if adapter is None:
            return []
        return await adapter.list_changed_files(review_run)
    except ProviderError as exc:
        warnings = list(review_run.validation_warnings_json or [])
        warnings.append(
            {
                "code": "changed_files_lookup_failed",
                "message": f"Using summary-only fallback: {exc}",
            }
        )
        review_run.validation_warnings_json = warnings
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)
    return []


async def _schedule_pi_agent_retry(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    settings: Settings,
) -> bool:
    if review_run.failure_code != "pi_agent_infrastructure_error":
        return False

    warnings = list(review_run.validation_warnings_json or [])
    if review_run.agent_session_id:
        if not any(
            isinstance(warning, dict)
            and warning.get("code") == "pi_agent_session_retry"
            for warning in warnings
        ):
            warnings.append(
                {
                    "code": "pi_agent_session_retry",
                    "message": review_run.error
                    or "pi-agent session request temporarily failed.",
                }
            )
        review_run.status = "running"
        review_run.stage = "waiting_for_agent"
        review_run.failure_code = None
        review_run.error = None
        review_run.completed_at = None
        review_run.validation_warnings_json = warnings
        review_run.lock_owner = None
        review_run.locked_until = utc_now() + timedelta(
            seconds=settings.worker_poll_interval_seconds
        )
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)
        return True

    retry_count = sum(
        1
        for warning in warnings
        if isinstance(warning, dict) and warning.get("code") == "pi_agent_start_retry"
    )
    if retry_count >= settings.retry_max_attempts:
        return False

    retry_number = retry_count + 1
    delay_seconds = settings.retry_initial_delay_seconds * (2**retry_count)
    warnings.append(
        {
            "code": "pi_agent_start_retry",
            "message": review_run.error
            or "pi-agent runtime infrastructure start failure.",
            "retry": retry_number,
        }
    )
    review_run.status = "running"
    review_run.stage = "retrying_agent_start"
    review_run.agent_session_id = None
    review_run.agent_status = None
    review_run.agent_provider = None
    review_run.agent_model = None
    review_run.agent_thinking_level = None
    review_run.failure_code = None
    review_run.error = None
    review_run.completed_at = None
    review_run.validation_warnings_json = warnings
    review_run.lock_owner = None
    review_run.locked_until = utc_now() + timedelta(seconds=delay_seconds)
    session.add(review_run)
    await session.commit()
    await session.refresh(review_run)
    return True


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
