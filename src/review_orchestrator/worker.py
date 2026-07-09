from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.comments import (
    publish_github_line_comments,
    publish_github_summary_comment,
)
from review_orchestrator.config import Settings
from review_orchestrator.github import GitHubClient, fetch_changed_files
from review_orchestrator.models import (
    AgentTask,
    ProviderEventInbox,
    PullRequestContext,
    ReviewRun,
    utc_now,
)
from review_orchestrator.openhands import OpenHandsClient
from review_orchestrator.schemas import ReviewRunCreate, WorkspacePrepareRequest
from review_orchestrator.services import (
    collect_review_result,
    create_review_run,
    get_or_create_review_config,
    start_review_session,
    sync_review_session,
)
from review_orchestrator.workspaces import prepare_workspace

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
) -> ReviewRun | None:
    now = now or utc_now()
    result = await session.execute(
        select(ReviewRun)
        .where(
            ReviewRun.status == "queued",
        )
        .order_by(ReviewRun.created_at)
        .limit(1)
    )
    review_run = result.scalar_one_or_none()
    if review_run is None:
        return None

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
) -> AgentTask | None:
    result = await session.execute(
        select(AgentTask)
        .where(AgentTask.status == "queued")
        .order_by(AgentTask.created_at)
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return None
    task.status = "running"
    task.result_json = {"worker_id": worker_id}
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def process_next_agent_task(
    session: AsyncSession,
    *,
    worker_id: str,
) -> AgentTask | None:
    task = await acquire_next_agent_task(session, worker_id=worker_id)
    if task is None:
        return None

    context = await _task_context(session, task)
    if context is None:
        task.status = "failed"
        task.error_message = "Pull request context was not found for mention task."
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task

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


async def process_next_review_run(
    session: AsyncSession,
    *,
    settings: Settings,
    openhands_client: OpenHandsClient,
    worker_id: str,
    github_client: GitHubClient | None = None,
) -> ReviewRun | None:
    review_run = await acquire_next_review_run(session, worker_id=worker_id)
    if review_run is None:
        return None

    context = await _review_run_context(session, review_run)
    if context is None:
        review_run.status = "failed"
        review_run.failure_code = "missing_pr_context"
        review_run.error = "Pull request context is required for automatic execution."
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)
        return review_run

    workspace = await prepare_workspace(
        session,
        settings,
        _workspace_request_from_context(context),
    )
    if workspace.status == "failed":
        review_run.status = "failed"
        review_run.failure_code = workspace.failure_code or "workspace_failed"
        review_run.error = workspace.failure_message
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)
        return review_run

    review_run = await start_review_session(
        session,
        review_run,
        openhands_client=openhands_client,
        workspace_path=workspace.workspace_path,
    )
    review_run = await sync_review_session(
        session,
        review_run,
        openhands_client=openhands_client,
    )
    raw_result = await _extract_openhands_json_result(
        openhands_client,
        review_run.openhands_conversation_id,
    )
    if raw_result is None:
        return review_run

    changed_files = []
    if github_client is not None and review_run.provider == "github":
        changed_files = await fetch_changed_files(
            github_client,
            repo_full_name=review_run.repo_full_name,
            pull_request_number=review_run.pull_request_number,
        )
    await collect_review_result(
        session,
        review_run,
        raw_output=raw_result,
        changed_files=changed_files,
    )
    config = await get_or_create_review_config(
        session,
        provider=review_run.provider,
        repo_full_name=review_run.repo_full_name,
    )
    if github_client is not None and review_run.provider == "github":
        await publish_github_summary_comment(
            session,
            review_run,
            github_client=github_client,
            status_text="completed",
            finding_stats=review_run.finding_count_by_severity,
        )
        if config.line_comments_enabled:
            await publish_github_line_comments(
                session,
                review_run,
                github_client=github_client,
                changed_files=changed_files,
            )
    await release_review_run_lock(session, review_run.id)
    return await session.get(ReviewRun, review_run.id)


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


async def _extract_openhands_json_result(
    openhands_client: OpenHandsClient,
    conversation_id: str | None,
) -> dict[str, Any] | None:
    if not conversation_id:
        return None
    page = await openhands_client.list_events(conversation_id, limit=100)
    for event in reversed(page.items):
        candidate = _extract_text(event)
        if not candidate:
            continue
        parsed = _try_json(candidate)
        if isinstance(parsed, dict) and "summary" in parsed and "findings" in parsed:
            return parsed
    return None


def _extract_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            text = _extract_text(value.get(key))
            if text:
                return text
        return None
    if isinstance(value, list):
        parts = [_extract_text(item) for item in value]
        joined = "\n".join(part for part in parts if part)
        return joined or None
    return None


def _try_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None
