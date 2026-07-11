from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.comments import (
    publish_github_line_comments,
    publish_github_summary_comment,
    publish_gitlab_summary_comment,
)
from review_orchestrator.config import Settings
from review_orchestrator.github import (
    GitHubClient,
    GitHubClientError,
    fetch_changed_files,
)
from review_orchestrator.gitlab import (
    GitLabClient,
    GitLabClientError,
    fetch_gitlab_changed_files,
)
from review_orchestrator.models import (
    AgentTask,
    ProviderEventInbox,
    PullRequestContext,
    ReviewRun,
    utc_now,
)
from review_orchestrator.openhands import OpenHandsClient, OpenHandsClientError
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
    github_client: GitHubClient | None = None,
    gitlab_client: GitLabClient | None = None,
) -> AgentTask | None:
    task = await acquire_next_agent_task(session, worker_id=worker_id)
    if task is None:
        return None

    try:
        context = await _task_context(session, task)
        if context is None:
            try:
                context = await _hydrate_task_context(
                    session,
                    task,
                    github_client=github_client,
                    gitlab_client=gitlab_client,
                )
            except (GitHubClientError, GitLabClientError) as exc:
                return await _fail_agent_task(
                    session,
                    task,
                    failure_code="provider_context_lookup_failed",
                    error=str(exc),
                    github_client=github_client,
                    gitlab_client=gitlab_client,
                )
        if context is None:
            return await _fail_agent_task(
                session,
                task,
                failure_code="missing_pr_context",
                error="Pull request context was not found for mention task.",
                github_client=github_client,
                gitlab_client=gitlab_client,
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
            github_client=github_client,
            gitlab_client=gitlab_client,
        )


async def _fail_agent_task(
    session: AsyncSession,
    task: AgentTask,
    *,
    failure_code: str,
    error: str,
    github_client: GitHubClient | None,
    gitlab_client: GitLabClient | None,
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
            github_client=github_client,
            gitlab_client=gitlab_client,
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
    openhands_client: OpenHandsClient,
    worker_id: str,
    github_client: GitHubClient | None = None,
    gitlab_client: GitLabClient | None = None,
) -> ReviewRun | None:
    review_run = await acquire_next_review_run(
        session,
        worker_id=worker_id,
        lock_seconds=settings.worker_lock_seconds,
    )
    if review_run is None:
        return None

    release_lock = True
    try:
        if _should_publish_reviewing(review_run):
            await publish_review_run_status_comment(
                session,
                review_run,
                github_client=github_client,
                gitlab_client=gitlab_client,
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
                status_text="failed",
            )
            return review_run

        if not review_run.workspace_path:
            try:
                workspace = await prepare_workspace(
                    session,
                    settings,
                    _workspace_request_from_context(context),
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
                    status_text="failed",
                )
                return review_run
            review_run.workspace_path = workspace.workspace_path
            session.add(review_run)
            await session.commit()
            await session.refresh(review_run)

        if (
            not review_run.openhands_start_task_id
            and not review_run.openhands_conversation_id
        ):
            review_run = await start_review_session(
                session,
                review_run,
                openhands_client=openhands_client,
                workspace_path=review_run.workspace_path,
            )
            if review_run.status == "failed":
                await publish_review_run_status_comment(
                    session,
                    review_run,
                    github_client=github_client,
                    gitlab_client=gitlab_client,
                    status_text="failed",
                )
                return review_run
        review_run = await sync_review_session(
            session,
            review_run,
            openhands_client=openhands_client,
        )
        if review_run.status == "failed":
            if await _schedule_openhands_start_retry(
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
                status_text="failed",
            )
            return review_run
        raw_result = await _extract_openhands_json_result(
            openhands_client,
            review_run.openhands_conversation_id,
        )
        if raw_result is None:
            review_run.stage = "waiting_for_result"
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
            review_run,
            github_client=github_client,
            gitlab_client=gitlab_client,
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
                status_text="failed",
            )
            return review_run
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
        if gitlab_client is not None and review_run.provider == "gitlab":
            await publish_gitlab_summary_comment(
                session,
                review_run,
                gitlab_client=gitlab_client,
                status_text="completed",
                finding_stats=review_run.finding_count_by_severity,
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


async def process_review_run_timeouts(
    session: AsyncSession,
    *,
    settings: Settings,
    openhands_client: OpenHandsClient,
    github_client: GitHubClient | None = None,
    gitlab_client: GitLabClient | None = None,
    now: datetime | None = None,
) -> list[ReviewRun]:
    now = now or utc_now()
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
            await _cancel_openhands_after_hard_timeout(
                session,
                refreshed,
                openhands_client=openhands_client,
            )
            await publish_review_run_status_comment(
                session,
                refreshed,
                github_client=github_client,
                gitlab_client=gitlab_client,
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
                status_text="delayed",
            )
            touched.append(refreshed)
    return touched


async def publish_review_run_status_comment(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    github_client: GitHubClient | None,
    gitlab_client: GitLabClient | None,
    status_text: str,
) -> None:
    original_failure_code = review_run.failure_code
    original_error = review_run.error
    if github_client is not None and review_run.provider == "github":
        await publish_github_summary_comment(
            session,
            review_run,
            github_client=github_client,
            status_text=status_text,
            finding_stats=review_run.finding_count_by_severity,
        )
    elif gitlab_client is not None and review_run.provider == "gitlab":
        await publish_gitlab_summary_comment(
            session,
            review_run,
            gitlab_client=gitlab_client,
            status_text=status_text,
            finding_stats=review_run.finding_count_by_severity,
        )
    else:
        return

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


async def _cancel_openhands_after_hard_timeout(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    openhands_client: OpenHandsClient,
) -> None:
    if not review_run.openhands_conversation_id:
        return
    try:
        await openhands_client.delete_conversation(review_run.openhands_conversation_id)
    except OpenHandsClientError as exc:
        review_run.error = (
            f"{review_run.error or 'Review run exceeded hard timeout.'} "
            f"OpenHands cleanup failed: {exc}"
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
    github_client: GitHubClient | None,
    gitlab_client: GitLabClient | None,
) -> PullRequestContext | None:
    if task.provider == "github" and github_client is not None:
        pull_request = await github_client.get_pull_request(
            task.repo_full_name,
            task.pull_request_number,
        )
        context = _context_from_github_pull_request(task, pull_request)
    elif task.provider == "gitlab" and gitlab_client is not None:
        merge_request = await gitlab_client.get_merge_request(
            task.repo_full_name,
            task.pull_request_number,
        )
        context = _context_from_gitlab_merge_request(task, merge_request)
    else:
        return None

    session.add(context)
    await session.commit()
    await session.refresh(context)
    task.pull_request_context_id = context.id
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return context


def _context_from_github_pull_request(
    task: AgentTask,
    pull_request: dict[str, Any],
) -> PullRequestContext:
    base = pull_request.get("base") if isinstance(pull_request, dict) else None
    head = pull_request.get("head") if isinstance(pull_request, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    head_repo = head.get("repo") if isinstance(head, dict) else None
    head_repo_full_name = _repo_full_name(head_repo)
    base_repo_full_name = _repo_full_name(base_repo)
    return PullRequestContext(
        provider=task.provider,
        repo_full_name=task.repo_full_name,
        pull_request_number=task.pull_request_number,
        provider_pr_id=_id_to_str(pull_request.get("id")),
        title=_str_or_none(pull_request.get("title")),
        author_login=_login(pull_request.get("user")),
        base_ref=_ref(base),
        base_sha=_sha(base),
        head_ref=_ref(head),
        head_sha=_sha(head) or "",
        head_repo_full_name=head_repo_full_name,
        is_fork=bool(
            head_repo_full_name and head_repo_full_name != base_repo_full_name
        ),
        status=_str_or_none(pull_request.get("state")) or "open",
        html_url=_str_or_none(pull_request.get("html_url")),
    )


def _context_from_gitlab_merge_request(
    task: AgentTask,
    merge_request: dict[str, Any],
) -> PullRequestContext:
    return PullRequestContext(
        provider=task.provider,
        repo_full_name=task.repo_full_name,
        pull_request_number=task.pull_request_number,
        provider_pr_id=_id_to_str(merge_request.get("id")),
        title=_str_or_none(merge_request.get("title")),
        author_login=_gitlab_author(merge_request.get("author")),
        base_ref=_str_or_none(merge_request.get("target_branch")),
        base_sha=_str_or_none(merge_request.get("diff_refs", {}).get("base_sha"))
        if isinstance(merge_request.get("diff_refs"), dict)
        else None,
        head_ref=_str_or_none(merge_request.get("source_branch")),
        head_sha=_str_or_none(merge_request.get("sha")) or "",
        head_repo_full_name=task.repo_full_name,
        is_fork=False,
        status=_str_or_none(merge_request.get("state")) or "opened",
        html_url=_str_or_none(merge_request.get("web_url")),
    )


async def _fetch_changed_files(
    review_run: ReviewRun,
    *,
    github_client: GitHubClient | None,
    gitlab_client: GitLabClient | None,
):
    try:
        if review_run.provider == "github" and github_client is not None:
            return await fetch_changed_files(
                github_client,
                repo_full_name=review_run.repo_full_name,
                pull_request_number=review_run.pull_request_number,
            )
        if review_run.provider == "gitlab" and gitlab_client is not None:
            return await fetch_gitlab_changed_files(
                gitlab_client,
                project_path=review_run.repo_full_name,
                merge_request_iid=review_run.pull_request_number,
            )
    except (GitHubClientError, GitLabClientError) as exc:
        warnings = list(review_run.validation_warnings_json or [])
        warnings.append(
            {
                "code": "changed_files_lookup_failed",
                "message": f"Using summary-only fallback: {exc}",
            }
        )
        review_run.validation_warnings_json = warnings
    return []


async def _extract_openhands_json_result(
    openhands_client: OpenHandsClient,
    conversation_id: str | None,
) -> dict[str, Any] | None:
    if not conversation_id:
        return None
    events: list[dict[str, Any]] = []
    page_id: str | None = None
    for _ in range(20):
        page = await openhands_client.list_events(
            conversation_id,
            page_id=page_id,
            limit=100,
        )
        events.extend(page.items)
        if not page.next_page_id:
            break
        page_id = page.next_page_id
    for event in reversed(events):
        for candidate in _extract_text_candidates(event):
            parsed = _try_json(candidate)
            if _is_review_result_payload(parsed):
                return parsed
    return None


async def _schedule_openhands_start_retry(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    settings: Settings,
) -> bool:
    if review_run.failure_code != "openhands_infrastructure_error":
        return False

    warnings = list(review_run.validation_warnings_json or [])
    if review_run.openhands_conversation_id:
        if not any(
            isinstance(warning, dict)
            and warning.get("code") == "openhands_session_retry"
            for warning in warnings
        ):
            warnings.append(
                {
                    "code": "openhands_session_retry",
                    "message": review_run.error
                    or "OpenHands session request temporarily failed.",
                }
            )
        review_run.status = "running"
        review_run.stage = "waiting_for_result"
        review_run.failure_code = None
        review_run.error = None
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
        if isinstance(warning, dict)
        and warning.get("code") == "openhands_start_retry"
    )
    if retry_count >= settings.retry_max_attempts:
        return False

    retry_number = retry_count + 1
    delay_seconds = settings.retry_initial_delay_seconds * (2**retry_count)
    warnings.append(
        {
            "code": "openhands_start_retry",
            "message": review_run.error or "OpenHands infrastructure start failure.",
            "retry": retry_number,
            "start_task_id": review_run.openhands_start_task_id,
        }
    )
    review_run.status = "running"
    review_run.stage = "retrying_openhands_start"
    review_run.openhands_start_task_id = None
    review_run.openhands_conversation_id = None
    review_run.openhands_sandbox_id = None
    review_run.openhands_agent_server_url = None
    review_run.failure_code = None
    review_run.error = None
    review_run.validation_warnings_json = warnings
    review_run.lock_owner = None
    review_run.locked_until = utc_now() + timedelta(seconds=delay_seconds)
    session.add(review_run)
    await session.commit()
    await session.refresh(review_run)
    return True


def _is_review_result_payload(value: Any) -> bool:
    return isinstance(value, dict) and "summary" in value and "findings" in value


def _extract_text_candidates(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        candidates: list[str] = []
        for key in ("text", "content", "message", "thought", "observation"):
            candidates.extend(_extract_text_candidates(value.get(key)))
        for key, item in value.items():
            if key not in {"text", "content", "message", "thought", "observation"}:
                candidates.extend(_extract_text_candidates(item))
        return candidates
    if isinstance(value, list):
        candidates = []
        for item in value:
            candidates.extend(_extract_text_candidates(item))
        return candidates
    return []


def _try_json(text: str) -> Any:
    stripped = text.strip()
    candidates = [stripped]
    candidates.extend(
        match.group("json").strip() for match in _FENCED_JSON_RE.finditer(stripped)
    )
    for candidate in candidates:
        parsed = _loads_json(candidate)
        if parsed is not None:
            return parsed
    return None


_FENCED_JSON_RE = re.compile(
    r"```(?:json|JSON)?\s*(?P<json>.*?)```",
    re.DOTALL,
)


def _loads_json(text: str) -> Any:
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _id_to_str(value: Any) -> str | None:
    if isinstance(value, int | str):
        return str(value)
    return None


def _sha(ref_object: Any) -> str | None:
    if not isinstance(ref_object, dict):
        return None
    return _str_or_none(ref_object.get("sha"))


def _ref(ref_object: Any) -> str | None:
    if not isinstance(ref_object, dict):
        return None
    return _str_or_none(ref_object.get("ref"))


def _repo_full_name(repo: Any) -> str | None:
    if not isinstance(repo, dict):
        return None
    return _str_or_none(repo.get("full_name"))


def _login(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return _str_or_none(user.get("login"))


def _gitlab_author(author: Any) -> str | None:
    if not isinstance(author, dict):
        return None
    return _str_or_none(author.get("username")) or _str_or_none(author.get("name"))
