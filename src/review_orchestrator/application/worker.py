"""Background review and agent-task execution."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.application.delivery import enqueue_delivery
from review_orchestrator.application.scheduler import (
    SchedulerPolicy,
    claim_next_task,
    policy_from_settings,
    release_task_claim,
)
from review_orchestrator.application.services import (
    ReviewRequest,
    collect_review_result,
    get_or_create_review_config,
    handle_review_requested,
    start_review_session,
    sync_review_session,
)
from review_orchestrator.application.session_archive import archive_agent_session
from review_orchestrator.domain.models import (
    AgentTask,
    ProviderEventInbox,
    PullRequestContext,
    ReviewRun,
    utc_now,
)
from review_orchestrator.domain.review_results import ChangedFile
from review_orchestrator.domain.schemas import WorkspacePrepareRequest
from review_orchestrator.infrastructure.config import Settings
from review_orchestrator.infrastructure.workspaces import prepare_workspace
from review_orchestrator.integrations.pi_agent import (
    AgentDomainPreset,
    AgentInstructionHistoryItem,
    AgentInstructionInput,
    AgentInstructionRepositoryContext,
    AgentTaskResult,
    PiAgentClient,
    PiAgentClientError,
)
from review_orchestrator.integrations.providers import (
    AgentTaskCommentsCapability,
    ChangedFilesCapability,
    LineCommentsCapability,
    ProviderError,
    ProviderRegistry,
    PullRequestCapability,
    ReviewSummaryCapability,
)

WORKER_ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "superseded"}
TIMEOUT_EVENTS = {
    "soft": "review_run.soft_timeout",
    "hard": "review_run.hard_timeout",
}


async def acquire_next_review_run(
    session: AsyncSession,
    *,
    worker_id: str,
    lock_seconds: int = 300,
    now: datetime | None = None,
    policy: SchedulerPolicy | None = None,
) -> ReviewRun | None:
    task = await claim_next_task(
        session,
        worker_id=worker_id,
        task_kinds={"review"},
        lock_seconds=lock_seconds,
        now=now,
        policy=policy,
    )
    if task is None:
        return None
    if not isinstance(task, ReviewRun):
        raise TypeError(f"Scheduler returned non-review task {task.id}.")
    return task


async def acquire_next_agent_task(
    session: AsyncSession,
    *,
    worker_id: str,
    lock_seconds: int = 300,
    now: datetime | None = None,
    policy: SchedulerPolicy | None = None,
) -> AgentTask | None:
    claimed = await claim_next_task(
        session,
        worker_id=worker_id,
        task_kinds={"agent"},
        lock_seconds=lock_seconds,
        now=now,
        policy=policy,
    )
    if claimed is None:
        return None
    if not isinstance(claimed, AgentTask):
        raise TypeError(f"Scheduler returned non-agent task {claimed.id}.")
    task = claimed
    if task.task_type != "message_command":
        task.result_json = {"worker_id": worker_id}
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
    provider_registry: ProviderRegistry | None = None,
) -> AgentTask | None:
    task = await acquire_next_agent_task(
        session,
        worker_id=worker_id,
        lock_seconds=settings.worker_lock_seconds if settings else 300,
        policy=policy_from_settings(settings) if settings else None,
    )
    if task is None:
        return None
    registry = provider_registry or ProviderRegistry()

    if task.task_type == "message_command":
        if registry.capability(task.provider, AgentTaskCommentsCapability) is None:
            return await _fail_command_agent_task(
                session,
                task,
                failure_code="provider_capability_missing",
                error=(
                    f"Provider {task.provider!r} does not support agent task "
                    "comment publishing."
                ),
                provider_registry=registry,
            )
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

        trigger_event = await _review_request_event_for_agent_task(
            session,
            task,
            context,
        )
        review_run, _ = await handle_review_requested(
            session,
            ReviewRequest(
                reason="mention",
                trigger_event_id=trigger_event.id,
                pull_request_context_id=context.id,
            ),
        )
        trigger_event.status = "queued"
        trigger_event.review_run_id = review_run.id
        trigger_event.processed_at = utc_now()
        task.status = "completed"
        task.result_json = {"review_run_id": review_run.id, "status": review_run.status}
        session.add(trigger_event)
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
    provider_registry: ProviderRegistry,
) -> AgentTask:
    if task.response_comment_id is None and task.stage == "placeholder_pending":
        task.status = "awaiting_delivery"
        task.stage = "placeholder_delivery_pending"
        await _enqueue_agent_task_comment(
            session,
            task,
            state="working",
            mandatory=True,
            success_status="queued",
            success_stage="placeholder_delivered",
        )
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
            AgentTask.status.in_({"queued", "running", "awaiting_delivery"}),
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
        await _enqueue_agent_task_comment(
            session,
            task,
            state="queued",
            mandatory=False,
        )
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
        default_command_skill=settings.agent_command_skill,
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
                provider_registry=provider_registry,
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
            preset = AgentDomainPreset(
                agent_id=settings.agent_command_agent,
                task_type="message-command",
                repository_skills=[command_config.default_agent_command_skill],
            )
            task.resolved_preset_json = {
                "schema_version": "1",
                "composition": {
                    "agent": {"id": preset.agent_id},
                    "repository": {"skills": preset.repository_skills},
                    "task_type": {"id": preset.task_type},
                },
            }
            runtime_session = await pi_agent_client.start_instruction_session(
                instruction,
                preset=preset,
            )
        except PiAgentClientError as exc:
            if (
                exc.infrastructure_failure
                and task.agent_start_attempts < settings.retry_max_attempts
            ):
                task.status = "queued"
                task.stage = "retrying_agent_start"
                task.error_message = str(exc)
                task.available_at = utc_now() + timedelta(
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

    if runtime_session.resolved_preset is not None:
        task.resolved_preset_json = runtime_session.resolved_preset
    await archive_agent_session(session, task, runtime_session)
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
    del provider_registry
    task.status = "awaiting_delivery"
    task.stage = "result_delivery_pending"
    task.execution_status = "completed"
    task.error_message = None
    task.failure_code = None
    await _enqueue_agent_task_comment(
        session,
        task,
        state="completed",
        mandatory=True,
        final_status="completed",
        final_stage="completed",
        supersede_pending=True,
    )
    return task


async def _enqueue_agent_task_comment(
    session: AsyncSession,
    task: AgentTask,
    *,
    state: str,
    mandatory: bool,
    success_status: str | None = None,
    success_stage: str | None = None,
    final_status: str | None = None,
    final_stage: str | None = None,
    supersede_pending: bool = False,
) -> None:
    payload: dict[str, Any] = {"state": state}
    if success_status is not None:
        payload["success_status"] = success_status
    if success_stage is not None:
        payload["success_stage"] = success_stage
    if final_status is not None:
        payload["final_status"] = final_status
    if final_stage is not None:
        payload["final_stage"] = final_stage
    await enqueue_delivery(
        session,
        task,
        provider=task.provider,
        operation="agent_task_comment",
        destination_key=f"{task.provider}:{task.repo_full_name}:pr:{task.pull_request_number}:task:{task.id}",
        idempotency_key=f"agent-task:{task.id}:comment:{state}:attempt:{task.attempt}",
        payload=payload,
        mandatory=mandatory,
        priority=task.priority,
        supersede_pending=supersede_pending,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)


async def _fail_command_agent_task(
    session: AsyncSession,
    task: AgentTask,
    *,
    failure_code: str,
    error: str,
    provider_registry: ProviderRegistry,
) -> AgentTask:
    del provider_registry
    task.status = "awaiting_delivery"
    task.stage = "failure_delivery_pending"
    task.execution_status = "failed"
    task.failure_code = failure_code
    task.error_message = error
    await _enqueue_agent_task_comment(
        session,
        task,
        state="failed",
        mandatory=True,
        final_status="failed",
        final_stage="failed",
        supersede_pending=True,
    )
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
            runtime_session = await pi_agent_client.cancel_session(
                task.agent_session_id
            )
            await archive_agent_session(session, task, runtime_session)
        except PiAgentClientError:
            pass
    del provider_registry
    task.status = "awaiting_delivery"
    task.stage = "cancellation_delivery_pending"
    task.execution_status = "cancelled"
    await _enqueue_agent_task_comment(
        session,
        task,
        state="cancelled",
        mandatory=True,
        final_status="cancelled",
        final_stage="cancelled",
        supersede_pending=True,
    )
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
    delay: float | None = None
    if task.stage != "retrying_agent_start" and task.status in {
        "queued",
        "running",
    } and task.stage in {
        "waiting_for_turn",
        "waiting_for_agent",
        "publishing_result",
        "publishing_failure",
        "cancellation_pending",
        "placeholder_pending",
    }:
        delay = retry_after_seconds
    released = await release_task_claim(
        session,
        task_id,
        retry_after_seconds=delay,
    )
    if released is None:
        return None
    if not isinstance(released, AgentTask):
        raise TypeError(f"Released non-agent task {released.id}.")
    return released


async def _fail_agent_task(
    session: AsyncSession,
    task: AgentTask,
    *,
    failure_code: str,
    error: str,
    provider_registry: ProviderRegistry,
) -> AgentTask:
    task.status = "failed"
    task.failure_code = failure_code
    task.error_message = error
    session.add(task)
    await session.commit()
    await session.refresh(task)

    context = await _task_context(session, task)
    if context is None:
        return task
    try:
        trigger_event = await _review_request_event_for_agent_task(
            session,
            task,
            context,
        )
        review_run, _ = await handle_review_requested(
            session,
            ReviewRequest(
                reason="mention",
                trigger_event_id=trigger_event.id,
                pull_request_context_id=context.id,
            ),
        )
        review_run.status = "failed"
        review_run.failure_code = failure_code
        review_run.error = error
        review_run.completed_at = utc_now()
        trigger_event.status = "queued"
        trigger_event.review_run_id = review_run.id
        trigger_event.processed_at = utc_now()
        session.add(trigger_event)
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)
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


async def _review_request_event_for_agent_task(
    session: AsyncSession,
    task: AgentTask,
    context: PullRequestContext,
) -> ProviderEventInbox:
    if task.provider_event_id:
        existing = await session.get(ProviderEventInbox, task.provider_event_id)
        if existing is not None:
            return existing
    delivery_id = f"agent-task:{task.id}:review-request"
    event = ProviderEventInbox(
        provider="internal",
        delivery_id=delivery_id,
        provider_event="review_request",
        provider_action="mention",
        internal_event="review_requested",
        repo_full_name=context.repo_full_name,
        pull_request_number=context.pull_request_number,
        head_sha=context.head_sha,
        dedupe_key=delivery_id,
        coalesce_key=f"review-request:agent-task:{task.id}",
        payload_digest=hashlib.sha256(delivery_id.encode()).hexdigest(),
        payload={
            "schema_version": "1",
            "trigger_type": "mention",
            "source_agent_task_id": task.id,
            "target_provider": context.provider,
        },
        status="received",
    )
    session.add(event)
    await session.flush()
    task.provider_event_id = event.id
    session.add(task)
    return event


async def process_next_review_run(
    session: AsyncSession,
    *,
    settings: Settings,
    pi_agent_client: PiAgentClient,
    worker_id: str,
    provider_registry: ProviderRegistry | None = None,
) -> ReviewRun | None:
    review_run = await acquire_next_review_run(
        session,
        worker_id=worker_id,
        lock_seconds=settings.worker_lock_seconds,
        policy=policy_from_settings(settings),
    )
    if review_run is None:
        return None
    registry = provider_registry or ProviderRegistry()

    try:
        registry.require_capability(
            review_run.provider,
            ReviewSummaryCapability,
            operation="publish_review_summary",
        )
    except ProviderError as exc:
        review_run.status = "failed"
        review_run.failure_code = "provider_capability_missing"
        review_run.error = str(exc)
        review_run.completed_at = utc_now()
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)
        await release_review_run_lock(session, review_run.id)
        return review_run

    try:
        if _should_publish_reviewing(review_run):
            await publish_review_run_status_comment(
                session,
                review_run,
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
                    provider_registry=registry,
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
                    return review_run
                await publish_review_run_status_comment(
                    session,
                    review_run,
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
                return review_run
            await publish_review_run_status_comment(
                session,
                review_run,
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
            review_run.available_at = utc_now() + timedelta(
                seconds=settings.worker_poll_interval_seconds
            )
            session.add(review_run)
            await session.commit()
            await session.refresh(review_run)
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
                provider_registry=registry,
                status_text="failed",
            )
            return review_run
        config = await get_or_create_review_config(
            session,
            provider=review_run.provider,
            repo_full_name=review_run.repo_full_name,
        )
        await publish_review_run_status_comment(
            session,
            review_run,
            provider_registry=registry,
            status_text="completed",
        )
        if config.line_comments_enabled:
            if registry.capability(
                review_run.provider,
                LineCommentsCapability,
            ) is not None:
                await _enqueue_review_line_comments(
                    session,
                    review_run,
                    changed_files=changed_files,
                )
            else:
                warnings = list(review_run.validation_warnings_json or [])
                warnings.append(
                    {
                        "code": "line_comments_unsupported",
                        "message": (
                            f"Provider {review_run.provider!r} does not support line "
                            "comments; the summary was queued for delivery."
                        ),
                    }
                )
                review_run.validation_warnings_json = warnings
                session.add(review_run)
                await session.commit()
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
            provider_registry=registry,
            status_text="failed",
        )
        return review_run
    finally:
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
    provider_registry: ProviderRegistry | None = None,
    now: datetime | None = None,
) -> list[AgentTask]:
    now = now or utc_now()
    registry = provider_registry or ProviderRegistry()
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
            await _enqueue_agent_task_comment(
                session,
                task,
                state="soft_timeout",
                mandatory=False,
            )
            touched.append(task)
    return touched


async def process_review_run_timeouts(
    session: AsyncSession,
    *,
    settings: Settings,
    pi_agent_client: PiAgentClient,
    provider_registry: ProviderRegistry | None = None,
    now: datetime | None = None,
) -> list[ReviewRun]:
    now = now or utc_now()
    registry = provider_registry or ProviderRegistry()
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
                provider_registry=registry,
                status_text="delayed",
            )
            touched.append(refreshed)
    return touched


async def publish_review_run_status_comment(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    provider_registry: ProviderRegistry | None = None,
    status_text: str,
) -> None:
    del provider_registry
    terminal = status_text in {"completed", "failed"}
    if terminal:
        review_run.execution_status = status_text
        review_run.status = "awaiting_delivery"
        review_run.stage = f"{status_text}_delivery_pending"
    payload: dict[str, Any] = {
        "status_text": status_text,
        "finding_stats": review_run.finding_count_by_severity,
    }
    if terminal:
        payload["final_status"] = status_text
        payload["final_stage"] = status_text
    await enqueue_delivery(
        session,
        review_run,
        provider=review_run.provider,
        operation="review_summary",
        destination_key=(
            f"{review_run.provider}:{review_run.repo_full_name}:"
            f"pr:{review_run.pull_request_number}:summary"
        ),
        idempotency_key=(
            f"review:{review_run.id}:summary:{status_text}:attempt:"
            f"{review_run.attempt}"
        ),
        payload=payload,
        mandatory=terminal,
        priority=review_run.priority,
        supersede_pending=terminal,
    )
    session.add(review_run)
    await session.commit()
    await session.refresh(review_run)


async def _enqueue_review_line_comments(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    changed_files: list[ChangedFile],
) -> None:
    await enqueue_delivery(
        session,
        review_run,
        provider=review_run.provider,
        operation="review_line_comments",
        destination_key=(
            f"{review_run.provider}:{review_run.repo_full_name}:"
            f"pr:{review_run.pull_request_number}:line-comments"
        ),
        idempotency_key=(
            f"review:{review_run.id}:line-comments:attempt:{review_run.attempt}"
        ),
        payload={
            "changed_files": [item.model_dump(mode="json") for item in changed_files],
            "final_status": "completed",
            "final_stage": "completed",
        },
        mandatory=True,
        priority=review_run.priority,
    )
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
    task = await release_task_claim(session, review_run_id)
    if task is None:
        return None
    if not isinstance(task, ReviewRun):
        raise TypeError(f"Released non-review task {task.id}.")
    return task


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
            },
            "pull_request": {
                "number": context.pull_request_number,
                "base_sha": context.base_sha,
                "head_sha": context.head_sha,
                "is_fork": context.is_fork,
            },
        }
    )


async def _hydrate_task_context(
    session: AsyncSession,
    task: AgentTask,
    *,
    provider_registry: ProviderRegistry,
) -> PullRequestContext | None:
    adapter = provider_registry.capability(task.provider, PullRequestCapability)
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
        adapter = provider_registry.capability(
            review_run.provider,
            ChangedFilesCapability,
        )
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
        review_run.available_at = utc_now() + timedelta(
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
    review_run.available_at = utc_now() + timedelta(seconds=delay_seconds)
    session.add(review_run)
    await session.commit()
    await session.refresh(review_run)
    return True


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
