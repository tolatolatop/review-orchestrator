"""Review lifecycle use cases, queries, and webhook orchestration."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.application.session_archive import archive_agent_session
from review_orchestrator.domain.models import (
    AgentTask,
    DeliveryOutbox,
    Finding,
    ProviderEventInbox,
    PullRequestContext,
    ResourceLease,
    ResourcePool,
    ReviewCommentRef,
    ReviewConfig,
    ReviewRun,
    ReviewSession,
    SessionArchive,
    Task,
    TaskAttempt,
    Workspace,
    utc_now,
)
from review_orchestrator.domain.reconciliation import persist_and_reconcile_findings
from review_orchestrator.domain.review_results import (
    ChangedFile,
    ParsedReviewResult,
    ReviewResultError,
    ReviewSkillInput,
    parse_review_result,
)
from review_orchestrator.domain.schemas import (
    AgentTaskDetail,
    AgentTaskListResponse,
    AgentTaskQueueHealth,
    AgentTaskSummary,
    DeliveryOutboxListResponse,
    DeliveryOutboxSummary,
    DeliverySchedulingUpdate,
    PiAgentSessionDiagnostics,
    ProviderEventInboxDetail,
    ProviderEventInboxListResponse,
    ProviderEventInboxSummary,
    ResourcePoolListResponse,
    ResourcePoolRead,
    ReviewRunCreate,
    ReviewRunDetail,
    ReviewRunFindingsSummary,
    ReviewRunLinkedEventSummary,
    ReviewRunLinkedTaskSummary,
    ReviewRunListItem,
    ReviewRunListResponse,
    ReviewRunOperationalState,
    ReviewRunProviderPublishing,
    ReviewRunPullRequestContext,
    ReviewRunRead,
    ReviewRunSessionSummary,
    ReviewRunWorkspaceSummary,
    SessionArchiveListResponse,
    SessionArchiveRead,
    TaskAttemptSummary,
    TaskListResponse,
    TaskSchedulingUpdate,
    TaskSummary,
    WebhookAccepted,
)
from review_orchestrator.infrastructure.config import Settings
from review_orchestrator.infrastructure.observability import (
    DEFAULT_OBSERVABILITY_SORT,
    redact_value,
)
from review_orchestrator.integrations.pi_agent import (
    AgentDomainPreset,
    PiAgentClient,
    PiAgentClientError,
    PiAgentSession,
    PiAgentSessionStatus,
)
from review_orchestrator.integrations.providers import (
    AgentCommand,
    AgentTaskCommentsCapability,
    ProviderRegistry,
    ProviderWebhookEvent,
    PullRequestSnapshot,
    ResourceLinksCapability,
    payload_digest,
)

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "superseded"}
CANCELLABLE_STATUSES = {"queued", "running"}


class ReviewRunTransitionError(ValueError):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def create_review_run(
    session: AsyncSession,
    payload: ReviewRunCreate,
    *,
    trigger_type: str = "manual",
    trigger_event_id: str | None = None,
) -> ReviewRun:
    latest = await get_latest_review_run_by_head(
        session,
        provider=payload.provider,
        repo_full_name=payload.repo_full_name,
        pull_request_number=payload.pull_request_number,
        head_sha=payload.head_sha,
    )
    if latest and not payload.force:
        return latest

    next_attempt = 1 if latest is None else latest.attempt + 1

    values = payload.model_dump(exclude={"force"})
    queue = "manual-review" if trigger_type == "manual" else "webhook-review"
    priority = 60 if trigger_type == "manual" else 40
    review_run = ReviewRun(
        **values,
        capability_id="code-review",
        status="queued",
        execution_status="pending",
        delivery_status="pending",
        queue=queue,
        priority=priority,
        effective_priority=priority,
        concurrency_key=(
            f"{payload.provider}:{payload.repo_full_name}:pr:"
            f"{payload.pull_request_number}:head:{payload.head_sha}"
        ),
        resource_context_json={
            "repository": f"{payload.provider}/{payload.repo_full_name}",
            "pr_head": (
                f"{payload.provider}/{payload.repo_full_name}/"
                f"{payload.pull_request_number}/{payload.head_sha}"
            ),
        },
        trigger_type=trigger_type,
        trigger_event_id=trigger_event_id,
        attempt=next_attempt,
    )
    session.add(review_run)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await get_latest_review_run_by_head(
            session,
            provider=payload.provider,
            repo_full_name=payload.repo_full_name,
            pull_request_number=payload.pull_request_number,
            head_sha=payload.head_sha,
        )
        if existing is None:
            raise
        return existing
    await session.commit()
    await session.refresh(review_run)
    return review_run


async def get_review_run(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewRun | None:
    return await session.get(ReviewRun, review_run_id)


async def get_pi_agent_session_diagnostics_for_review_run(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    pi_agent_client: PiAgentClient | None = None,
    live_status_disabled_reason: str | None = None,
) -> PiAgentSessionDiagnostics:
    agent_task_ids = await _get_agent_task_ids_for_review_run(session, review_run)
    execution_status: str | None = None
    execution_stage: str | None = None
    event_count = 0
    live_status_error = live_status_disabled_reason

    if review_run.agent_session_id and pi_agent_client is not None:
        try:
            runtime_session = await pi_agent_client.get_session(
                review_run.agent_session_id
            )
        except PiAgentClientError as exc:
            live_status_error = str(exc)
        else:
            execution_status = runtime_session.status
            execution_stage = runtime_session.stage
            event_count = len(runtime_session.events)
            live_status_error = None

    return PiAgentSessionDiagnostics(
        review_run_id=review_run.id,
        agent_task_ids=agent_task_ids,
        provider=review_run.provider,
        repo_full_name=review_run.repo_full_name,
        pull_request_number=review_run.pull_request_number,
        status=review_run.status,
        stage=review_run.stage,
        agent_session_id=review_run.agent_session_id,
        agent_provider=review_run.agent_provider,
        agent_model=review_run.agent_model,
        agent_thinking_level=review_run.agent_thinking_level,
        execution_status=execution_status,
        execution_stage=execution_stage,
        event_count=event_count,
        session_available=bool(review_run.agent_session_id),
        live_status_available=execution_status is not None,
        live_status_error=live_status_error,
        created_at=review_run.created_at,
        updated_at=review_run.updated_at,
    )


async def get_pi_agent_session_diagnostics_for_agent_task(
    task: AgentTask,
    *,
    pi_agent_client: PiAgentClient | None = None,
    live_status_disabled_reason: str | None = None,
) -> PiAgentSessionDiagnostics:
    execution_status: str | None = None
    execution_stage: str | None = None
    event_count = 0
    live_status_error = live_status_disabled_reason
    if task.agent_session_id and pi_agent_client is not None:
        try:
            runtime_session = await pi_agent_client.get_session(task.agent_session_id)
        except PiAgentClientError as exc:
            live_status_error = str(exc)
        else:
            execution_status = runtime_session.status
            execution_stage = runtime_session.stage
            event_count = len(runtime_session.events)
            live_status_error = None
    return PiAgentSessionDiagnostics(
        review_run_id=None,
        agent_task_ids=[task.id],
        provider=task.provider,
        repo_full_name=task.repo_full_name,
        pull_request_number=task.pull_request_number,
        status=task.status,
        stage=task.stage,
        agent_session_id=task.agent_session_id,
        agent_provider=task.agent_provider,
        agent_model=task.agent_model,
        agent_thinking_level=task.agent_thinking_level,
        execution_status=execution_status,
        execution_stage=execution_stage,
        event_count=event_count,
        session_available=bool(task.agent_session_id),
        live_status_available=execution_status is not None,
        live_status_error=live_status_error,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def get_pi_agent_session_diagnostics_for_session(
    session: AsyncSession,
    agent_session_id: str,
    *,
    pi_agent_client: PiAgentClient | None = None,
    live_status_disabled_reason: str | None = None,
) -> PiAgentSessionDiagnostics | None:
    result = await session.execute(
        select(ReviewRun)
        .where(ReviewRun.agent_session_id == agent_session_id)
        .order_by(ReviewRun.created_at.desc(), ReviewRun.id.desc())
        .limit(1)
    )
    review_run = result.scalar_one_or_none()
    if review_run is not None:
        return await get_pi_agent_session_diagnostics_for_review_run(
            session,
            review_run,
            pi_agent_client=pi_agent_client,
            live_status_disabled_reason=live_status_disabled_reason,
        )
    task_result = await session.execute(
        select(AgentTask)
        .where(AgentTask.agent_session_id == agent_session_id)
        .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
        .limit(1)
    )
    task = task_result.scalar_one_or_none()
    if task is None:
        return None
    return await get_pi_agent_session_diagnostics_for_agent_task(
        task,
        pi_agent_client=pi_agent_client,
        live_status_disabled_reason=live_status_disabled_reason,
    )


async def _get_agent_task_ids_for_review_run(
    session: AsyncSession,
    review_run: ReviewRun,
) -> list[str]:
    result = await session.execute(
        select(AgentTask).where(
            AgentTask.provider == review_run.provider,
            AgentTask.repo_full_name == review_run.repo_full_name,
            AgentTask.pull_request_number == review_run.pull_request_number,
        )
    )
    task_ids: list[str] = []
    for task in result.scalars():
        result_json = task.result_json or {}
        if (
            task.provider_event_id == review_run.trigger_event_id
            or result_json.get("review_run_id") == review_run.id
        ):
            task_ids.append(task.id)
    return task_ids


async def list_review_runs(
    session: AsyncSession,
    *,
    provider: str | None = None,
    repo_full_name: str | None = None,
    pull_request_number: int | None = None,
    merge_request_number: int | None = None,
    status: str | None = None,
    stage: str | None = None,
    head_sha: str | None = None,
    trigger_type: str | None = None,
    lock_state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ReviewRunListResponse:
    filters = _review_run_filters(
        provider=provider,
        repo_full_name=repo_full_name,
        pull_request_number=pull_request_number or merge_request_number,
        status=status,
        stage=stage,
        head_sha=head_sha,
        trigger_type=trigger_type,
        lock_state=lock_state,
    )
    total_result = await session.execute(
        select(func.count()).select_from(ReviewRun).where(*filters)
    )
    total = int(total_result.scalar_one())

    result = await session.execute(
        select(ReviewRun)
        .where(*filters)
        .order_by(ReviewRun.created_at.desc(), ReviewRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    review_runs = list(result.scalars().all())
    publishing_by_run_id = await _provider_publishing_for_runs(
        session, [review_run.id for review_run in review_runs]
    )
    contexts_by_id, contexts_by_identity = await _pull_request_contexts_for_runs(
        session, review_runs
    )
    return ReviewRunListResponse(
        items=[
            _review_run_list_item(
                review_run,
                publishing_by_run_id.get(review_run.id),
                contexts_by_id.get(review_run.pull_request_context_id)
                or contexts_by_identity.get(_review_run_identity(review_run)),
            )
            for review_run in review_runs
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_review_run_detail(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewRunDetail | None:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        return None

    publishing = await _provider_publishing_for_run(session, review_run.id)
    context = await _pull_request_context_for_run(session, review_run)
    workspace = await _workspace_for_run(session, review_run)
    review_session = await _review_session_for_run(session, review_run.id)
    findings = await _findings_summary_for_run(session, review_run.id)
    trigger_event = await _trigger_event_for_run(session, review_run)
    agent_task = await _agent_task_for_run(session, review_run)
    list_item = _review_run_list_item(review_run, publishing, context)

    return ReviewRunDetail(
        **list_item.model_dump(),
        workspace=_workspace_summary(workspace, review_run.workspace_path),
        review_session=_review_session_summary(review_session),
        findings_summary=findings,
        validation_warnings=review_run.validation_warnings_json or [],
        validation_errors=review_run.validation_errors_json or [],
        trigger_event=_linked_event_summary(trigger_event),
        agent_task=_linked_task_summary(agent_task),
    )


async def start_review_session(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    pi_agent_client: PiAgentClient,
    settings: Settings,
    workspace_path: str | None = None,
) -> ReviewRun:
    if review_run.status in {"cancelled", "superseded", "completed"}:
        raise ReviewRunTransitionError(
            f"Review run {review_run.id} cannot be started from {review_run.status}."
        )

    resolved_workspace_path = workspace_path or review_run.workspace_path
    if not resolved_workspace_path:
        raise ReviewRunTransitionError("workspace_path is required to start review.")
    if not review_run.base_sha:
        raise ReviewRunTransitionError("base_sha is required to start review.")

    review_input = ReviewSkillInput(
        provider=review_run.provider,
        repo_full_name=review_run.repo_full_name,
        pr_number=review_run.pull_request_number,
        base_sha=review_run.base_sha,
        head_sha=review_run.head_sha,
        workspace_path=resolved_workspace_path,
    )
    review_config = await get_or_create_review_config(
        session,
        provider=review_run.provider,
        repo_full_name=review_run.repo_full_name,
        default_skill=settings.pi_agent_review_skill,
    )
    preset = AgentDomainPreset(
        agent_id=settings.pi_agent_review_agent,
        task_type="code-review",
        repository_skills=[review_config.default_review_skill],
    )
    review_run.resolved_preset_json = {
        "schema_version": "1",
        "composition": {
            "agent": {"id": preset.agent_id},
            "repository": {"skills": preset.repository_skills},
            "task_type": {"id": preset.task_type},
        },
    }
    try:
        runtime_session = await pi_agent_client.start_session(
            review_input,
            preset=preset,
        )
    except PiAgentClientError as exc:
        review_run.status = "failed"
        review_run.failure_code = (
            "pi_agent_infrastructure_error"
            if exc.infrastructure_failure
            else "pi_agent_error"
        )
        review_run.error = str(exc)
        review_run.completed_at = utc_now()
        await session.commit()
        await session.refresh(review_run)
        return review_run

    review_run.status = "running"
    review_run.started_at = utc_now()
    review_run.completed_at = None
    review_run.workspace_path = resolved_workspace_path
    if runtime_session.resolved_preset is not None:
        review_run.resolved_preset_json = runtime_session.resolved_preset
    _copy_pi_agent_state(review_run, runtime_session)
    if runtime_session.resolved_preset is not None:
        review_run.resolved_preset_json = runtime_session.resolved_preset
    if runtime_session.result is not None:
        review_run.result_raw_json = runtime_session.result
    review_run.failure_code = None
    review_run.error = None
    review_session = await _review_session_for_run(session, review_run.id)
    if review_session is None:
        review_session = ReviewSession(review_run_id=review_run.id)
        session.add(review_session)
    review_session.agent_session_id = runtime_session.id
    review_session.status = runtime_session.status
    review_session.skill_name = review_config.default_review_skill
    review_session.profile_name = _runtime_profile(runtime_session)
    review_session.input_snapshot_json = review_input.model_dump()
    await archive_agent_session(session, review_run, runtime_session)
    await session.commit()
    await session.refresh(review_run)
    return review_run


async def sync_review_session(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    pi_agent_client: PiAgentClient,
) -> ReviewRun:
    if review_run.status in {"cancelled", "superseded", "completed", "failed"}:
        return review_run

    if not review_run.agent_session_id:
        return await _mark_failed(
            session,
            review_run,
            "pi-agent session id is missing.",
            failure_code="pi_agent_error",
        )
    try:
        runtime_session = await pi_agent_client.get_session(review_run.agent_session_id)
    except PiAgentClientError as exc:
        return await _mark_failed(
            session,
            review_run,
            str(exc),
            failure_code=(
                "pi_agent_infrastructure_error"
                if exc.infrastructure_failure
                else "pi_agent_error"
            ),
        )

    _copy_pi_agent_state(review_run, runtime_session)
    if runtime_session.result is not None:
        review_run.result_raw_json = runtime_session.result
    review_session = await _review_session_for_run(session, review_run.id)
    if review_session is not None:
        review_session.status = runtime_session.status
        review_session.error_message = runtime_session.error
        if runtime_session.result is not None:
            review_session.result_ref = f"pi-agent:{runtime_session.id}:result"
        session.add(review_session)

    if runtime_session.status == PiAgentSessionStatus.failed:
        review_run = await _mark_failed(
            session,
            review_run,
            runtime_session.error or "pi-agent review failed.",
            failure_code="pi_agent_error",
        )
        await archive_agent_session(session, review_run, runtime_session)
        await session.commit()
        return review_run
    if runtime_session.status == PiAgentSessionStatus.cancelled:
        review_run.status = "cancelled"
        review_run.stage = "cancelled"
        review_run.completed_at = utc_now()
    elif runtime_session.status == PiAgentSessionStatus.completed:
        review_run.status = "running"
        review_run.stage = "agent_completed"
    else:
        review_run.status = "running"
        review_run.stage = runtime_session.stage

    await archive_agent_session(session, review_run, runtime_session)
    await session.commit()
    await session.refresh(review_run)
    return review_run


async def cancel_review_session(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    pi_agent_client: PiAgentClient,
    reason: str,
) -> ReviewRun:
    if review_run.status in {"completed", "cancelled", "superseded"}:
        return review_run

    if review_run.agent_session_id:
        try:
            runtime_session = await pi_agent_client.cancel_session(
                review_run.agent_session_id
            )
        except PiAgentClientError as exc:
            review_run.error = f"Cancel requested; pi-agent cleanup failed: {exc}"
        else:
            _copy_pi_agent_state(review_run, runtime_session)
            review_run.error = reason
            await archive_agent_session(session, review_run, runtime_session)
    else:
        review_run.error = reason

    review_run.status = "cancelled"
    review_run.stage = "cancelled"
    review_run.completed_at = utc_now()
    review_session = await _review_session_for_run(session, review_run.id)
    if review_session is not None:
        review_session.status = "cancelled"
        review_session.error_message = review_run.error
        session.add(review_session)
    await session.commit()
    await session.refresh(review_run)
    return review_run


def _copy_pi_agent_state(
    review_run: ReviewRun,
    runtime_session: PiAgentSession,
) -> None:
    review_run.agent_session_id = runtime_session.id
    review_run.agent_status = runtime_session.status
    review_run.agent_provider = runtime_session.provider
    review_run.agent_model = runtime_session.model
    review_run.agent_thinking_level = runtime_session.thinking_level


def _runtime_profile(runtime_session: PiAgentSession) -> str | None:
    preset = runtime_session.resolved_preset
    if not isinstance(preset, dict):
        return runtime_session.profile
    composition = preset.get("composition")
    if not isinstance(composition, dict):
        return runtime_session.profile
    task_type = composition.get("task_type")
    if not isinstance(task_type, dict):
        return runtime_session.profile
    profile = task_type.get("profile")
    return profile if isinstance(profile, str) else runtime_session.profile


async def collect_review_result(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    raw_output: str | dict[str, Any],
    changed_files: list[ChangedFile] | None = None,
) -> ParsedReviewResult:
    if not review_run.base_sha:
        raise ReviewRunTransitionError("base_sha is required to collect review result.")

    try:
        parsed = parse_review_result(
            raw_output,
            changed_files=changed_files,
            provider=review_run.provider,
            repo_full_name=review_run.repo_full_name,
            pr_number=review_run.pull_request_number,
            base_sha=review_run.base_sha,
            head_sha=review_run.head_sha,
        )
    except ReviewResultError as exc:
        review_run.status = "failed"
        review_run.failure_code = "invalid_result"
        review_run.error = f"{exc.code}: {exc.message}"
        await session.commit()
        raise

    await persist_and_reconcile_findings(session, review_run, parsed)
    review_run.status = "completed"
    review_run.stage = "completed"
    review_run.review_summary = parsed.result.summary
    review_run.failure_code = None
    review_run.error = None
    review_run.completed_at = utc_now()
    review_session = await _review_session_for_run(session, review_run.id)
    if review_session is not None:
        review_session.status = "completed"
        review_session.result_ref = f"review-run:{review_run.id}:result"
        review_session.error_message = None
        session.add(review_session)
    await session.commit()
    await session.refresh(review_run)
    return parsed


async def get_review_run_by_head(
    session: AsyncSession,
    *,
    provider: str,
    repo_full_name: str,
    pull_request_number: int,
    head_sha: str,
    attempt: int,
) -> ReviewRun | None:
    result = await session.execute(
        select(ReviewRun).where(
            ReviewRun.provider == provider,
            ReviewRun.repo_full_name == repo_full_name,
            ReviewRun.pull_request_number == pull_request_number,
            ReviewRun.head_sha == head_sha,
            ReviewRun.attempt == attempt,
        )
    )
    return result.scalar_one_or_none()


async def get_latest_review_run_by_head(
    session: AsyncSession,
    *,
    provider: str,
    repo_full_name: str,
    pull_request_number: int,
    head_sha: str,
) -> ReviewRun | None:
    result = await session.execute(
        select(ReviewRun)
        .where(
            ReviewRun.provider == provider,
            ReviewRun.repo_full_name == repo_full_name,
            ReviewRun.pull_request_number == pull_request_number,
            ReviewRun.head_sha == head_sha,
        )
        .order_by(ReviewRun.attempt.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def retry_review_run(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewRun | None:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        return None
    if review_run.status != "failed":
        return review_run
    payload = ReviewRunCreate(
        provider=review_run.provider,
        repo_full_name=review_run.repo_full_name,
        pull_request_number=review_run.pull_request_number,
        base_sha=review_run.base_sha,
        head_sha=review_run.head_sha,
        force=True,
    )
    return await create_review_run(session, payload, trigger_type="retry")


async def cancel_review_run(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewRun | None:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        return None
    if review_run.status in CANCELLABLE_STATUSES:
        review_run.status = "cancelled"
        review_run.failure_code = "cancelled"
        session.add(review_run)
        await session.commit()
        await session.refresh(review_run)
    return review_run


async def accept_provider_webhook(
    session: AsyncSession,
    *,
    delivery_id: str,
    provider_event: str,
    normalized_event: ProviderWebhookEvent,
    payload: dict[str, Any],
    raw_body: bytes,
) -> WebhookAccepted:
    provider = normalized_event.provider
    existing_event = await get_provider_event(session, provider, delivery_id)
    if existing_event:
        return WebhookAccepted(
            provider=provider,
            delivery_id=delivery_id,
            status=existing_event.status,
            internal_event=existing_event.internal_event,
            review_run_id=existing_event.review_run_id,
            agent_task_id=await _get_agent_task_id_for_event(
                session, existing_event.id
            ),
            duplicate=True,
        )

    event = ProviderEventInbox(
        provider=provider,
        delivery_id=delivery_id,
        provider_event=provider_event,
        provider_action=normalized_event.provider_action,
        internal_event=normalized_event.internal_event,
        repo_full_name=normalized_event.repository,
        pull_request_number=normalized_event.pull_request_number,
        head_sha=normalized_event.head_sha,
        dedupe_key=f"{provider}:{delivery_id}",
        coalesce_key=_build_coalesce_key(
            provider,
            normalized_event.repository,
            normalized_event.pull_request_number,
            normalized_event.head_sha,
        ),
        payload_digest=payload_digest(raw_body),
        payload=payload,
        status=normalized_event.status,
    )
    session.add(event)
    await session.flush()

    review_run_id: str | None = None
    agent_task_id: str | None = None
    context: PullRequestContext | None = None
    if normalized_event.should_update_context:
        if normalized_event.pull_request is None:
            raise ValueError(
                f"Provider {provider!r} did not supply normalized PR context."
            )
        context = await upsert_pull_request_context(
            session,
            event,
            normalized_event.pull_request,
        )

    if normalized_event.internal_event in {"pr_closed", "pr_merged"}:
        await cancel_active_review_runs_for_pr(
            session,
            provider=provider,
            repo_full_name=event.repo_full_name,
            pull_request_number=event.pull_request_number,
            failure_code=normalized_event.internal_event,
        )
        await cancel_active_agent_tasks_for_pr(
            session,
            provider=provider,
            repo_full_name=event.repo_full_name,
            pull_request_number=event.pull_request_number,
            failure_code=normalized_event.internal_event,
        )

    if normalized_event.should_create_review_run:
        if normalized_event.pull_request is None:
            raise ValueError(
                f"Provider {provider!r} did not supply normalized PR context."
            )
        review_run = await create_review_run_from_snapshot(
            session,
            event.provider,
            normalized_event.pull_request,
            trigger_event_id=event.id,
        )
        await supersede_older_review_runs(session, review_run)
        review_run_id = review_run.id
        event.review_run_id = review_run_id
        event.status = "queued"
        if context is not None:
            context.latest_review_run_id = review_run_id

    if normalized_event.should_create_agent_task:
        if normalized_event.agent_command is None:
            raise ValueError("Agent command event is missing command metadata.")
        agent_task = await create_agent_task_from_event(
            session,
            event,
            command=normalized_event.agent_command,
            context=context,
        )
        agent_task_id = agent_task.id
        event.status = "queued"

    if event.status == "received":
        event.status = "processed"
    event.processed_at = utc_now()

    await session.commit()
    return WebhookAccepted(
        provider=provider,
        delivery_id=delivery_id,
        status=event.status,
        internal_event=event.internal_event,
        review_run_id=review_run_id,
        agent_task_id=agent_task_id,
    )


async def get_provider_event(
    session: AsyncSession,
    provider: str,
    delivery_id: str,
) -> ProviderEventInbox | None:
    result = await session.execute(
        select(ProviderEventInbox).where(
            ProviderEventInbox.provider == provider,
            ProviderEventInbox.delivery_id == delivery_id,
        )
    )
    return result.scalar_one_or_none()


async def list_provider_event_inbox(
    session: AsyncSession,
    *,
    provider: str | None = None,
    repo_full_name: str | None = None,
    pull_request_number: int | None = None,
    internal_event: str | None = None,
    status: str | None = None,
    delivery_id: str | None = None,
    created_from: Any | None = None,
    created_to: Any | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ProviderEventInboxListResponse:
    filters = _provider_event_filters(
        provider=provider,
        repo_full_name=repo_full_name,
        pull_request_number=pull_request_number,
        internal_event=internal_event,
        status=status,
        delivery_id=delivery_id,
        created_from=created_from,
        created_to=created_to,
    )
    total_result = await session.execute(
        select(func.count()).select_from(ProviderEventInbox).where(*filters)
    )
    total = int(total_result.scalar_one())

    result = await session.execute(
        select(ProviderEventInbox)
        .where(*filters)
        .order_by(ProviderEventInbox.created_at.desc(), ProviderEventInbox.id.desc())
        .limit(limit)
        .offset(offset)
    )
    events = list(result.scalars().all())
    agent_task_ids = await _get_agent_task_ids_for_events(
        session, [event.id for event in events]
    )
    return ProviderEventInboxListResponse(
        items=[
            _provider_event_summary(event, agent_task_ids.get(event.id))
            for event in events
        ],
        total=total,
        limit=limit,
        offset=offset,
        sort=DEFAULT_OBSERVABILITY_SORT,
    )


async def get_provider_event_inbox_detail(
    session: AsyncSession,
    event_id: str,
    *,
    include_payload: bool = False,
) -> ProviderEventInboxDetail | None:
    event = await session.get(ProviderEventInbox, event_id)
    if event is None:
        return None
    agent_task_id = await _get_agent_task_id_for_event(session, event.id)
    return ProviderEventInboxDetail(
        **_provider_event_summary(event, agent_task_id).model_dump(),
        dedupe_key=event.dedupe_key,
        payload=redact_value(event.payload) if include_payload else None,
    )


async def upsert_pull_request_context(
    session: AsyncSession,
    event: ProviderEventInbox,
    snapshot: PullRequestSnapshot,
) -> PullRequestContext | None:
    repository_name = snapshot.repository
    pull_request_number = snapshot.number

    result = await session.execute(
        select(PullRequestContext).where(
            PullRequestContext.provider == event.provider,
            PullRequestContext.repo_full_name == repository_name,
            PullRequestContext.pull_request_number == pull_request_number,
        )
    )
    context = result.scalar_one_or_none()
    if context is None:
        context = PullRequestContext(
            provider=event.provider,
            repo_full_name=repository_name,
            pull_request_number=pull_request_number,
            head_sha=snapshot.head_sha,
        )
        session.add(context)

    context.provider_repo_id = snapshot.provider_repo_id
    context.provider_pr_id = snapshot.provider_pr_id
    context.title = snapshot.title
    context.author_login = snapshot.author_login
    context.base_ref = snapshot.base_ref
    context.base_sha = snapshot.base_sha
    context.head_ref = snapshot.head_ref
    context.head_sha = snapshot.head_sha
    context.head_repo_full_name = snapshot.head_repo_full_name
    context.is_fork = bool(
        context.head_repo_full_name
        and context.head_repo_full_name != snapshot.base_repo_full_name
    )
    context.status = snapshot.status
    context.html_url = snapshot.html_url
    context.latest_event_id = event.id
    context.closed_at = snapshot.closed_at
    context.merged_at = snapshot.merged_at

    return context


async def create_agent_task_from_event(
    session: AsyncSession,
    event: ProviderEventInbox,
    *,
    command: AgentCommand,
    context: PullRequestContext | None = None,
) -> AgentTask:
    if not event.repo_full_name or event.pull_request_number is None:
        raise ValueError("Agent task event is missing PR identity fields.")

    if context is None:
        result = await session.execute(
            select(PullRequestContext).where(
                PullRequestContext.provider == event.provider,
                PullRequestContext.repo_full_name == event.repo_full_name,
                PullRequestContext.pull_request_number == event.pull_request_number,
            )
        )
        context = result.scalar_one_or_none()

    agent_task = AgentTask(
        capability_id="pr-assistant",
        provider_event_id=event.id,
        pull_request_context_id=context.id if context else None,
        provider=event.provider,
        repo_full_name=event.repo_full_name,
        pull_request_number=event.pull_request_number,
        task_type="message_command",
        status="queued",
        execution_status="pending",
        delivery_status="pending",
        queue="interactive",
        priority=80,
        effective_priority=80,
        concurrency_key=(
            f"{event.provider}:{event.repo_full_name}:pr:"
            f"{event.pull_request_number}"
        ),
        resource_context_json={
            "user": command.author_login,
            "repository": f"{event.provider}/{event.repo_full_name}",
            "pr": (
                f"{event.provider}/{event.repo_full_name}/"
                f"{event.pull_request_number}"
            ),
            "comment": (
                f"{event.provider}/{command.source_comment_id}"
                if command.source_comment_id
                else f"{event.provider}/event/{event.id}"
            ),
        },
        stage="placeholder_pending",
        source_kind=command.source_kind,
        source_comment_id=command.source_comment_id,
        source_url=command.source_url,
        source_author_login=command.author_login,
        command_text=command.command_text,
        head_sha=context.head_sha if context else event.head_sha,
        input_json={
            "internal_event": event.internal_event,
            "provider_action": event.provider_action,
            "author_association": command.author_association,
            "payload_digest": event.payload_digest,
        },
    )
    session.add(agent_task)
    await session.flush()
    return agent_task


async def list_tasks(
    session: AsyncSession,
    *,
    kind: str | None = None,
    capability_id: str | None = None,
    status: str | None = None,
    queue: str | None = None,
    resource_class: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> TaskListResponse:
    filters = []
    if kind:
        filters.append(Task.kind == kind)
    if capability_id:
        filters.append(Task.capability_id == capability_id)
    if status:
        filters.append(Task.status == status)
    if queue:
        filters.append(Task.queue == queue)
    if resource_class:
        filters.append(Task.resource_class == resource_class)

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(Task).where(*filters)
            )
        ).scalar_one()
    )
    tasks = list(
        (
            await session.execute(
                select(Task)
                .where(*filters)
                .order_by(
                    Task.effective_priority.desc(),
                    Task.available_at,
                    Task.created_at,
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
    )
    return TaskListResponse(
        items=[_task_summary(task) for task in tasks],
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_task_summary(
    session: AsyncSession,
    task_id: str,
) -> TaskSummary | None:
    task = await session.get(Task, task_id)
    return None if task is None else _task_summary(task)


async def list_task_session_archives(
    session: AsyncSession,
    task_id: str,
) -> SessionArchiveListResponse | None:
    if await session.get(Task, task_id) is None:
        return None
    archives = list(
        (
            await session.execute(
                select(SessionArchive)
                .where(SessionArchive.task_id == task_id)
                .order_by(SessionArchive.created_at, SessionArchive.id)
            )
        ).scalars()
    )
    attempts = await _attempts_for_archives(session, archives)
    return SessionArchiveListResponse(
        items=[_session_archive_read(item, attempts) for item in archives]
    )


async def get_session_archive(
    session: AsyncSession,
    archive_id: str,
) -> SessionArchiveRead | None:
    archive = await session.get(SessionArchive, archive_id)
    if archive is None:
        return None
    attempts = await _attempts_for_archives(session, [archive])
    return _session_archive_read(archive, attempts)


async def _attempts_for_archives(
    session: AsyncSession,
    archives: list[SessionArchive],
) -> dict[str, TaskAttempt]:
    attempt_ids = {
        archive.task_attempt_id
        for archive in archives
        if archive.task_attempt_id is not None
    }
    if not attempt_ids:
        return {}
    attempts = list(
        (
            await session.execute(
                select(TaskAttempt).where(TaskAttempt.id.in_(attempt_ids))
            )
        ).scalars()
    )
    return {attempt.id: attempt for attempt in attempts}


def _session_archive_read(
    archive: SessionArchive,
    attempts: dict[str, TaskAttempt],
) -> SessionArchiveRead:
    attempt = (
        attempts.get(archive.task_attempt_id)
        if archive.task_attempt_id is not None
        else None
    )
    attempt_summary = None
    if attempt is not None:
        attempt_summary = TaskAttemptSummary(
            id=attempt.id,
            task_id=attempt.task_id,
            attempt_no=attempt.attempt_no,
            status=attempt.status,
            stage=attempt.stage,
            agent_run_id=attempt.agent_run_id,
            workspace_id=attempt.workspace_id,
            workspace_path=attempt.workspace_path,
            resolved_preset=attempt.resolved_preset_json,
            usage=attempt.usage_json,
            failure_category=attempt.failure_category,
            error_message=_safe_error_message(attempt.error_message),
            started_at=attempt.started_at,
            completed_at=attempt.completed_at,
        )
    return SessionArchiveRead(
        id=archive.id,
        task_id=archive.task_id,
        task_attempt=attempt_summary,
        agent_run_id=archive.agent_run_id,
        session=archive.session_json,
        task_metadata=archive.task_metadata_json,
        workspace_diff=archive.workspace_diff,
        workspace_diff_truncated=archive.workspace_diff_truncated,
        redaction_version=archive.redaction_version,
        created_at=archive.created_at,
        updated_at=archive.updated_at,
    )


async def update_task_scheduling(
    session: AsyncSession,
    task_id: str,
    payload: TaskSchedulingUpdate,
) -> TaskSummary | None:
    task = await session.get(Task, task_id)
    if task is None:
        return None
    if task.status in TERMINAL_STATUSES:
        raise ReviewRunTransitionError(
            f"Task is already {task.status}; scheduling cannot be changed."
        )
    if (
        task.lock_owner is not None
        and task.locked_until is not None
        and not _is_past(task.locked_until)
    ):
        raise ReviewRunTransitionError(
            "Task scheduling cannot be changed while a worker lease is active."
        )
    values = payload.model_dump(exclude_none=True)
    if "queue" in values:
        task.queue = values["queue"]
    if "priority" in values:
        task.priority = values["priority"]
        task.effective_priority = values["priority"]
    if "available_at" in values:
        task.available_at = values["available_at"]
    if "resource_class" in values:
        task.resource_class = values["resource_class"]
    if "resource_context" in values:
        task.resource_context_json = values["resource_context"]
    task.updated_at = utc_now()
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return _task_summary(task)


async def list_resource_pools(session: AsyncSession) -> ResourcePoolListResponse:
    pools = list(
        (
            await session.execute(
                select(ResourcePool).order_by(
                    ResourcePool.dimension, ResourcePool.resource_key
                )
            )
        ).scalars()
    )
    active = {
        key: int(units)
        for key, units in (
            await session.execute(
                select(
                    ResourceLease.resource_key,
                    func.coalesce(func.sum(ResourceLease.units), 0),
                )
                .where(ResourceLease.expires_at > utc_now())
                .group_by(ResourceLease.resource_key)
            )
        ).all()
    }
    return ResourcePoolListResponse(
        items=[
            ResourcePoolRead(
                resource_key=pool.resource_key,
                dimension=pool.dimension,
                capacity=pool.capacity,
                active_units=active.get(pool.resource_key, 0),
                created_at=pool.created_at,
                updated_at=pool.updated_at,
            )
            for pool in pools
        ]
    )


async def set_resource_pool_capacity(
    session: AsyncSession,
    resource_key: str,
    *,
    capacity: int,
    dimension: str | None = None,
) -> ResourcePoolRead:
    pool = await session.get(ResourcePool, resource_key)
    if pool is None:
        inferred_dimension = resource_key.partition(":")[0]
        pool = ResourcePool(
            resource_key=resource_key,
            dimension=dimension or inferred_dimension or "custom",
            capacity=capacity,
        )
    elif dimension is not None and dimension != pool.dimension:
        raise ReviewRunTransitionError(
            "An existing Resource Pool dimension cannot be changed."
        )
    pool.capacity = capacity
    pool.updated_at = utc_now()
    session.add(pool)
    await session.commit()
    active_units = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(ResourceLease.units), 0)).where(
                    ResourceLease.resource_key == resource_key,
                    ResourceLease.expires_at > utc_now(),
                )
            )
        ).scalar_one()
    )
    await session.refresh(pool)
    return ResourcePoolRead(
        resource_key=pool.resource_key,
        dimension=pool.dimension,
        capacity=pool.capacity,
        active_units=active_units,
        created_at=pool.created_at,
        updated_at=pool.updated_at,
    )


async def list_delivery_outbox(
    session: AsyncSession,
    *,
    task_id: str | None = None,
    provider: str | None = None,
    status: str | None = None,
    queue: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DeliveryOutboxListResponse:
    filters = []
    if task_id:
        filters.append(DeliveryOutbox.task_id == task_id)
    if provider:
        filters.append(DeliveryOutbox.provider == provider)
    if status:
        filters.append(DeliveryOutbox.status == status)
    if queue:
        filters.append(DeliveryOutbox.queue == queue)
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(DeliveryOutbox).where(*filters)
            )
        ).scalar_one()
    )
    deliveries = list(
        (
            await session.execute(
                select(DeliveryOutbox)
                .where(*filters)
                .order_by(
                    DeliveryOutbox.priority.desc(),
                    DeliveryOutbox.available_at,
                    DeliveryOutbox.created_at,
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
    )
    return DeliveryOutboxListResponse(
        items=[_delivery_summary(item) for item in deliveries],
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_delivery_outbox(
    session: AsyncSession,
    delivery_id: str,
) -> DeliveryOutboxSummary | None:
    delivery = await session.get(DeliveryOutbox, delivery_id)
    return None if delivery is None else _delivery_summary(delivery)


async def update_delivery_scheduling(
    session: AsyncSession,
    delivery_id: str,
    payload: DeliverySchedulingUpdate,
) -> DeliveryOutboxSummary | None:
    delivery = await session.get(DeliveryOutbox, delivery_id)
    if delivery is None:
        return None
    if delivery.status not in {"queued", "failed"}:
        raise ReviewRunTransitionError(
            f"Delivery is {delivery.status}; scheduling cannot be changed."
        )
    values = payload.model_dump(exclude_none=True)
    for field_name, value in values.items():
        setattr(delivery, field_name, value)
    if delivery.status == "failed":
        delivery.status = "queued"
        delivery.last_error = None
    delivery.lock_owner = None
    delivery.locked_until = None
    delivery.updated_at = utc_now()
    session.add(delivery)
    await session.commit()
    await session.refresh(delivery)
    return _delivery_summary(delivery)


def _delivery_summary(delivery: DeliveryOutbox) -> DeliveryOutboxSummary:
    return DeliveryOutboxSummary(
        id=delivery.id,
        task_id=delivery.task_id,
        provider=delivery.provider,
        operation=delivery.operation,
        destination_key=delivery.destination_key,
        idempotency_key=delivery.idempotency_key,
        mandatory=delivery.mandatory,
        status=delivery.status,
        queue=delivery.queue,
        priority=delivery.priority,
        available_at=delivery.available_at,
        attempt=delivery.attempt,
        max_attempts=delivery.max_attempts,
        provider_message_id=delivery.provider_message_id,
        last_error=delivery.last_error,
        delivered_at=delivery.delivered_at,
        created_at=delivery.created_at,
        updated_at=delivery.updated_at,
    )


def _task_summary(task: Task) -> TaskSummary:
    metadata: dict[str, Any] = {}
    if isinstance(task, ReviewRun):
        metadata = {
            "provider": task.provider,
            "repo_full_name": task.repo_full_name,
            "pull_request_number": task.pull_request_number,
            "head_sha": task.head_sha,
            "trigger_type": task.trigger_type,
        }
    elif isinstance(task, AgentTask):
        metadata = {
            "provider": task.provider,
            "repo_full_name": task.repo_full_name,
            "pull_request_number": task.pull_request_number,
            "task_type": task.task_type,
            "source_comment_id": task.source_comment_id,
        }
    return TaskSummary(
        id=task.id,
        kind=task.kind,
        capability_id=task.capability_id,
        status=task.status,
        stage=task.stage,
        execution_status=task.execution_status,
        delivery_status=task.delivery_status,
        queue=task.queue,
        priority=task.priority,
        effective_priority=task.effective_priority,
        available_at=task.available_at,
        deadline_at=task.deadline_at,
        dedupe_key=task.dedupe_key,
        concurrency_key=task.concurrency_key,
        resource_class=task.resource_class,
        resource_context=task.resource_context_json,
        max_attempts=task.max_attempts,
        lock_owner=task.lock_owner,
        locked_until=task.locked_until,
        domain_metadata=metadata,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


async def list_agent_tasks(
    session: AsyncSession,
    *,
    provider_registry: ProviderRegistry | None = None,
    status: str | None = None,
    provider: str | None = None,
    repo_full_name: str | None = None,
    pull_request_number: int | None = None,
    task_type: str | None = None,
    created_from: Any | None = None,
    created_to: Any | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AgentTaskListResponse:
    filters = _agent_task_filters(
        status=status,
        provider=provider,
        repo_full_name=repo_full_name,
        pull_request_number=pull_request_number,
        task_type=task_type,
        created_from=created_from,
        created_to=created_to,
    )
    total_result = await session.execute(
        select(func.count()).select_from(AgentTask).where(*filters)
    )
    total = int(total_result.scalar_one())

    result = await session.execute(
        select(AgentTask)
        .where(*filters)
        .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
        .limit(limit)
        .offset(offset)
    )
    tasks = list(result.scalars().all())

    health_filters = _agent_task_filters(
        status=None,
        provider=provider,
        repo_full_name=repo_full_name,
        pull_request_number=pull_request_number,
        task_type=task_type,
        created_from=created_from,
        created_to=created_to,
    )
    return AgentTaskListResponse(
        items=[
            _agent_task_summary(task, provider_registry=provider_registry)
            for task in tasks
        ],
        total=total,
        limit=limit,
        offset=offset,
        queue=await _agent_task_queue_health(session, health_filters),
    )


async def get_agent_task_detail(
    session: AsyncSession,
    task_id: str,
    *,
    provider_registry: ProviderRegistry | None = None,
) -> AgentTaskDetail | None:
    task = await session.get(AgentTask, task_id)
    if task is None:
        return None
    return AgentTaskDetail(
        **_agent_task_summary(
            task,
            provider_registry=provider_registry,
        ).model_dump(),
        input_metadata=redact_value(task.input_json),
        result_json=task.result_json,
    )


async def cancel_agent_task(
    session: AsyncSession,
    task_id: str,
    *,
    pi_agent_client: PiAgentClient,
    provider_registry: ProviderRegistry,
) -> AgentTask | None:
    task = await session.get(AgentTask, task_id)
    if task is None:
        return None
    if task.task_type != "message_command":
        raise ReviewRunTransitionError("Only message-command tasks can be cancelled.")
    if task.status not in {"queued", "running"}:
        raise ReviewRunTransitionError(
            f"Agent task cannot be cancelled from status {task.status}."
        )
    task.status = "running"
    task.stage = "cancellation_pending"
    session.add(task)
    await session.commit()
    if task.agent_session_id:
        try:
            await pi_agent_client.cancel_session(task.agent_session_id)
        except PiAgentClientError:
            pass
    adapter = provider_registry.capability(
        task.provider,
        AgentTaskCommentsCapability,
    )
    if adapter is None:
        raise ReviewRunTransitionError(
            f"Provider {task.provider} cannot publish agent task comments.",
            status_code=503,
        )
    try:
        await adapter.publish_agent_task_comment(session, task, state="cancelled")
    except Exception as exc:
        task.last_publish_error = str(exc)
        session.add(task)
        await session.commit()
        raise ReviewRunTransitionError(str(exc), status_code=503) from exc
    task.status = "cancelled"
    task.stage = "cancelled"
    task.completed_at = utc_now()
    task.lock_owner = None
    task.locked_until = None
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def retry_agent_task(
    session: AsyncSession,
    task_id: str,
) -> AgentTask | None:
    task = await session.get(AgentTask, task_id)
    if task is None:
        return None
    if task.task_type != "message_command":
        raise ReviewRunTransitionError("Only message-command tasks can be retried.")
    if task.status != "failed":
        raise ReviewRunTransitionError(
            f"Agent task cannot be retried from status {task.status}."
        )
    task.status = "queued"
    task.stage = "placeholder_pending"
    task.attempt += 1
    task.agent_start_attempts = 0
    task.agent_session_id = None
    task.agent_status = None
    task.result_text = None
    task.result_json = None
    task.failure_code = None
    task.error_message = None
    task.last_publish_error = None
    task.response_body_hash = None
    task.started_at = None
    task.completed_at = None
    task.deadline_at = None
    task.soft_timeout_emitted_at = None
    task.hard_timeout_emitted_at = None
    task.lock_owner = None
    task.locked_until = None
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _get_agent_task_id_for_event(
    session: AsyncSession,
    provider_event_id: str,
) -> str | None:
    result = await session.execute(
        select(AgentTask.id)
        .where(AgentTask.provider_event_id == provider_event_id)
        .order_by(AgentTask.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_agent_task_ids_for_events(
    session: AsyncSession,
    provider_event_ids: list[str],
) -> dict[str, str]:
    if not provider_event_ids:
        return {}
    result = await session.execute(
        select(AgentTask.provider_event_id, AgentTask.id)
        .where(AgentTask.provider_event_id.in_(provider_event_ids))
        .order_by(AgentTask.created_at.desc())
    )
    task_ids: dict[str, str] = {}
    for provider_event_id, task_id in result.all():
        if provider_event_id is not None and provider_event_id not in task_ids:
            task_ids[provider_event_id] = task_id
    return task_ids


def _agent_task_filters(
    *,
    status: str | None,
    provider: str | None,
    repo_full_name: str | None,
    pull_request_number: int | None,
    task_type: str | None,
    created_from: Any | None,
    created_to: Any | None,
) -> list[Any]:
    filters: list[Any] = []
    if status:
        filters.append(AgentTask.status == status)
    if provider:
        filters.append(AgentTask.provider == provider)
    if repo_full_name:
        filters.append(AgentTask.repo_full_name == repo_full_name)
    if pull_request_number is not None:
        filters.append(AgentTask.pull_request_number == pull_request_number)
    if task_type:
        filters.append(AgentTask.task_type == task_type)
    if created_from is not None:
        filters.append(AgentTask.created_at >= created_from)
    if created_to is not None:
        filters.append(AgentTask.created_at <= created_to)
    return filters


async def _agent_task_queue_health(
    session: AsyncSession,
    filters: list[Any],
) -> AgentTaskQueueHealth:
    count_result = await session.execute(
        select(AgentTask.status, func.count())
        .where(*filters)
        .group_by(AgentTask.status)
    )
    counts = {status: int(count) for status, count in count_result.all()}
    oldest_result = await session.execute(
        select(func.min(AgentTask.created_at)).where(
            *filters,
            AgentTask.status == "queued",
        )
    )
    oldest_queued_at = oldest_result.scalar_one()
    oldest_queued_age_seconds = None
    if oldest_queued_at is not None:
        if oldest_queued_at.tzinfo is None:
            oldest_queued_at = oldest_queued_at.replace(tzinfo=UTC)
        oldest_queued_age_seconds = max(
            0,
            int((utc_now() - oldest_queued_at).total_seconds()),
        )
    return AgentTaskQueueHealth(
        queued=counts.get("queued", 0),
        running=counts.get("running", 0),
        completed=counts.get("completed", 0),
        failed=counts.get("failed", 0),
        cancelled=counts.get("cancelled", 0),
        oldest_queued_age_seconds=oldest_queued_age_seconds,
    )


def _agent_task_summary(
    task: AgentTask,
    *,
    provider_registry: ProviderRegistry | None = None,
) -> AgentTaskSummary:
    link_builder = (
        provider_registry.capability(task.provider, ResourceLinksCapability)
        if provider_registry is not None
        else None
    )
    return AgentTaskSummary(
        id=task.id,
        provider=task.provider,
        repo_full_name=task.repo_full_name,
        pull_request_number=task.pull_request_number,
        task_type=task.task_type,
        status=task.status,
        stage=task.stage,
        source_kind=task.source_kind,
        source_comment_id=task.source_comment_id,
        source_url=task.source_url,
        source_author_login=task.source_author_login,
        command_text=task.command_text,
        head_sha=task.head_sha,
        response_comment_id=task.response_comment_id,
        response_comment_url=(
            link_builder.agent_task_comment_url(task)
            if link_builder is not None
            else None
        ),
        agent_session_id=task.agent_session_id,
        agent_status=task.agent_status,
        agent_provider=task.agent_provider,
        agent_model=task.agent_model,
        agent_thinking_level=task.agent_thinking_level,
        failure_code=task.failure_code,
        provider_event_id=task.provider_event_id,
        provider_event_link=(
            f"/api/v1/provider-events/{task.provider_event_id}"
            if task.provider_event_id
            else None
        ),
        pull_request_context_link=(
            f"/api/v1/pull-request-contexts/{task.pull_request_context_id}"
            if task.pull_request_context_id
            else None
        ),
        error_message=_safe_error_message(task.error_message),
        started_at=task.started_at,
        completed_at=task.completed_at,
        deadline_at=task.deadline_at,
        soft_timeout_emitted_at=task.soft_timeout_emitted_at,
        hard_timeout_emitted_at=task.hard_timeout_emitted_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _provider_event_filters(
    *,
    provider: str | None,
    repo_full_name: str | None,
    pull_request_number: int | None,
    internal_event: str | None,
    status: str | None,
    delivery_id: str | None,
    created_from: Any | None,
    created_to: Any | None,
) -> list[Any]:
    filters: list[Any] = []
    if provider:
        filters.append(ProviderEventInbox.provider == provider)
    if repo_full_name:
        filters.append(ProviderEventInbox.repo_full_name == repo_full_name)
    if pull_request_number is not None:
        filters.append(ProviderEventInbox.pull_request_number == pull_request_number)
    if internal_event:
        filters.append(ProviderEventInbox.internal_event == internal_event)
    if status:
        filters.append(ProviderEventInbox.status == status)
    if delivery_id:
        filters.append(ProviderEventInbox.delivery_id == delivery_id)
    if created_from is not None:
        filters.append(ProviderEventInbox.created_at >= created_from)
    if created_to is not None:
        filters.append(ProviderEventInbox.created_at <= created_to)
    return filters


def _provider_event_summary(
    event: ProviderEventInbox,
    agent_task_id: str | None,
) -> ProviderEventInboxSummary:
    return ProviderEventInboxSummary(
        id=event.id,
        provider=event.provider,
        delivery_id=event.delivery_id,
        provider_event=event.provider_event,
        provider_action=event.provider_action,
        internal_event=event.internal_event,
        status=event.status,
        repo_full_name=event.repo_full_name,
        pull_request_number=event.pull_request_number,
        head_sha=event.head_sha,
        payload_digest=event.payload_digest,
        coalesce_key=event.coalesce_key,
        review_run_id=event.review_run_id,
        agent_task_id=agent_task_id,
        error_code=event.error_code,
        error_message=_safe_error_message(event.error_message),
        created_at=event.created_at,
        processed_at=event.processed_at,
    )


def _review_run_filters(
    *,
    provider: str | None,
    repo_full_name: str | None,
    pull_request_number: int | None,
    status: str | None,
    stage: str | None,
    head_sha: str | None,
    trigger_type: str | None,
    lock_state: str | None,
) -> list[Any]:
    filters: list[Any] = []
    if provider:
        filters.append(ReviewRun.provider == provider)
    if repo_full_name:
        filters.append(ReviewRun.repo_full_name == repo_full_name)
    if pull_request_number is not None:
        filters.append(ReviewRun.pull_request_number == pull_request_number)
    if status:
        filters.append(ReviewRun.status == status)
    if stage:
        filters.append(ReviewRun.stage == stage)
    if head_sha:
        filters.append(ReviewRun.head_sha == head_sha)
    if trigger_type:
        filters.append(ReviewRun.trigger_type == trigger_type)
    if lock_state == "unlocked":
        filters.append(ReviewRun.lock_owner.is_(None))
    elif lock_state == "locked":
        now = utc_now()
        filters.append(ReviewRun.lock_owner.is_not(None))
        filters.append(
            (ReviewRun.locked_until.is_(None)) | (ReviewRun.locked_until > now)
        )
    elif lock_state == "expired":
        filters.append(ReviewRun.lock_owner.is_not(None))
        filters.append(ReviewRun.locked_until <= utc_now())
    return filters


def _review_run_list_item(
    review_run: ReviewRun,
    publishing: ReviewRunProviderPublishing | None,
    context: PullRequestContext | None = None,
) -> ReviewRunListItem:
    base = ReviewRunRead.model_validate(review_run).model_dump()
    base["error"] = _safe_error_message(review_run.error)
    return ReviewRunListItem(
        **base,
        operational_state=_operational_state(review_run),
        provider_publishing=publishing
        or ReviewRunProviderPublishing(
            summary_comment_id=review_run.summary_comment_id,
            summary_published=review_run.summary_comment_id is not None,
        ),
        pull_request_context=_pull_request_context_summary(context),
    )


def _operational_state(review_run: ReviewRun) -> ReviewRunOperationalState:
    return ReviewRunOperationalState(
        lock_state=_lock_state(review_run),
        timeout_state=_timeout_state(review_run),
        worker_state=_worker_state(review_run),
    )


def _lock_state(review_run: ReviewRun) -> str:
    if review_run.lock_owner is None:
        return "unlocked"
    if review_run.locked_until is not None and _is_past(review_run.locked_until):
        return "expired"
    return "locked"


def _timeout_state(review_run: ReviewRun) -> str:
    if review_run.hard_timeout_emitted_at is not None:
        return "hard_timeout"
    if review_run.soft_timeout_emitted_at is not None:
        return "soft_timeout"
    if review_run.deadline_at is not None and _is_past(review_run.deadline_at):
        return "deadline_elapsed"
    return "none"


def _is_past(value: datetime) -> bool:
    now = utc_now()
    if value.tzinfo is None:
        now = now.replace(tzinfo=None)
    return value <= now


def _worker_state(review_run: ReviewRun) -> str:
    if review_run.status in TERMINAL_STATUSES:
        return "terminal"
    lock_state = _lock_state(review_run)
    if lock_state == "locked":
        return "locked_by_worker"
    if lock_state == "expired":
        return "worker_lock_expired"
    if review_run.status == "queued":
        return "waiting_for_worker"
    if review_run.status == "running" and not review_run.agent_session_id:
        return "waiting_for_agent"
    if review_run.status == "running":
        return "running_in_pi_agent"
    return review_run.status


async def _provider_publishing_for_runs(
    session: AsyncSession,
    review_run_ids: list[str],
) -> dict[str, ReviewRunProviderPublishing]:
    if not review_run_ids:
        return {}

    result = await session.execute(
        select(ReviewCommentRef).where(
            ReviewCommentRef.review_run_id.in_(review_run_ids)
        )
    )
    refs_by_run_id: dict[str, list[ReviewCommentRef]] = {}
    for ref in result.scalars().all():
        if ref.review_run_id is not None:
            refs_by_run_id.setdefault(ref.review_run_id, []).append(ref)
    return {
        review_run_id: _provider_publishing_from_refs(refs)
        for review_run_id, refs in refs_by_run_id.items()
    }


async def _provider_publishing_for_run(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewRunProviderPublishing:
    return (await _provider_publishing_for_runs(session, [review_run_id])).get(
        review_run_id, ReviewRunProviderPublishing()
    )


def _provider_publishing_from_refs(
    refs: list[ReviewCommentRef],
) -> ReviewRunProviderPublishing:
    summary_ref = next((ref for ref in refs if ref.comment_type == "summary"), None)
    line_status_counts: dict[str, int] = {}
    line_comment_count = 0
    for ref in refs:
        if ref.comment_type != "line":
            continue
        line_comment_count += 1
        line_status_counts[ref.status] = line_status_counts.get(ref.status, 0) + 1

    return ReviewRunProviderPublishing(
        summary_comment_id=summary_ref.provider_comment_id if summary_ref else None,
        summary_comment_ref_id=summary_ref.id if summary_ref else None,
        summary_comment_status=summary_ref.status if summary_ref else None,
        summary_published=summary_ref is not None,
        line_comment_count=line_comment_count,
        line_comment_status_counts=line_status_counts,
    )


async def _pull_request_context_for_run(
    session: AsyncSession,
    review_run: ReviewRun,
) -> PullRequestContext | None:
    if review_run.pull_request_context_id:
        context = await session.get(
            PullRequestContext,
            review_run.pull_request_context_id,
        )
        if context is not None:
            return context
    result = await session.execute(
        select(PullRequestContext).where(
            PullRequestContext.provider == review_run.provider,
            PullRequestContext.repo_full_name == review_run.repo_full_name,
            PullRequestContext.pull_request_number == review_run.pull_request_number,
        )
    )
    return result.scalar_one_or_none()


async def _pull_request_contexts_for_runs(
    session: AsyncSession,
    review_runs: list[ReviewRun],
) -> tuple[
    dict[str, PullRequestContext],
    dict[tuple[str, str, int], PullRequestContext],
]:
    if not review_runs:
        return {}, {}

    context_ids = {
        review_run.pull_request_context_id
        for review_run in review_runs
        if review_run.pull_request_context_id
    }
    identities = {_review_run_identity(review_run) for review_run in review_runs}
    predicates = [
        tuple_(
            PullRequestContext.provider,
            PullRequestContext.repo_full_name,
            PullRequestContext.pull_request_number,
        ).in_(identities)
    ]
    if context_ids:
        predicates.append(PullRequestContext.id.in_(context_ids))

    result = await session.execute(select(PullRequestContext).where(or_(*predicates)))
    contexts = list(result.scalars().all())
    return (
        {context.id: context for context in contexts},
        {
            (context.provider, context.repo_full_name, context.pull_request_number): (
                context
            )
            for context in contexts
        },
    )


def _review_run_identity(review_run: ReviewRun) -> tuple[str, str, int]:
    return (
        review_run.provider,
        review_run.repo_full_name,
        review_run.pull_request_number,
    )


async def _workspace_for_run(
    session: AsyncSession,
    review_run: ReviewRun,
) -> Workspace | None:
    result = await session.execute(
        select(Workspace)
        .where(
            Workspace.provider == review_run.provider,
            Workspace.repository == review_run.repo_full_name,
            Workspace.pull_request_number == review_run.pull_request_number,
            Workspace.head_sha == review_run.head_sha,
        )
        .order_by(Workspace.updated_at.desc(), Workspace.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _review_session_for_run(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewSession | None:
    result = await session.execute(
        select(ReviewSession).where(ReviewSession.review_run_id == review_run_id)
    )
    return result.scalar_one_or_none()


async def _findings_summary_for_run(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewRunFindingsSummary:
    result = await session.execute(
        select(Finding).where(Finding.review_run_id == review_run_id)
    )
    findings = list(result.scalars().all())
    by_severity: dict[str, int] = {}
    by_state: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for finding in findings:
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
        by_state[finding.state] = by_state.get(finding.state, 0) + 1
        by_status[finding.status] = by_status.get(finding.status, 0) + 1
    return ReviewRunFindingsSummary(
        total=len(findings),
        by_severity=by_severity,
        by_state=by_state,
        by_status=by_status,
    )


async def _trigger_event_for_run(
    session: AsyncSession,
    review_run: ReviewRun,
) -> ProviderEventInbox | None:
    if review_run.trigger_event_id:
        return await session.get(ProviderEventInbox, review_run.trigger_event_id)
    result = await session.execute(
        select(ProviderEventInbox)
        .where(ProviderEventInbox.review_run_id == review_run.id)
        .order_by(ProviderEventInbox.created_at.desc(), ProviderEventInbox.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _agent_task_for_run(
    session: AsyncSession,
    review_run: ReviewRun,
) -> AgentTask | None:
    result = await session.execute(
        select(AgentTask)
        .where(
            AgentTask.provider == review_run.provider,
            AgentTask.repo_full_name == review_run.repo_full_name,
            AgentTask.pull_request_number == review_run.pull_request_number,
        )
        .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _pull_request_context_summary(
    context: PullRequestContext | None,
) -> ReviewRunPullRequestContext | None:
    if context is None:
        return None
    return ReviewRunPullRequestContext(
        id=context.id,
        title=context.title,
        author_login=context.author_login,
        base_ref=context.base_ref,
        base_sha=context.base_sha,
        head_ref=context.head_ref,
        head_sha=context.head_sha,
        head_repo_full_name=context.head_repo_full_name,
        is_fork=context.is_fork,
        status=context.status,
        html_url=context.html_url,
        latest_event_id=context.latest_event_id,
        closed_at=context.closed_at,
        merged_at=context.merged_at,
    )


def _workspace_summary(
    workspace: Workspace | None,
    review_run_workspace_path: str | None,
) -> ReviewRunWorkspaceSummary | None:
    if workspace is None and review_run_workspace_path is None:
        return None
    if workspace is None:
        return ReviewRunWorkspaceSummary(workspace_path=review_run_workspace_path)
    return ReviewRunWorkspaceSummary(
        workspace_id=workspace.workspace_id,
        workspace_path=workspace.workspace_path,
        status=workspace.status,
        failure_code=workspace.failure_code,
        failure_message=_safe_error_message(workspace.failure_message),
        ready_at=workspace.ready_at,
        last_used_at=workspace.last_used_at,
        expires_at=workspace.expires_at,
    )


def _review_session_summary(
    review_session: ReviewSession | None,
) -> ReviewRunSessionSummary | None:
    if review_session is None:
        return None
    return ReviewRunSessionSummary(
        id=review_session.id,
        status=review_session.status,
        agent_session_id=review_session.agent_session_id,
        skill_name=review_session.skill_name,
        profile_name=review_session.profile_name,
        result_ref=review_session.result_ref,
        error_message=_safe_error_message(review_session.error_message),
        created_at=review_session.created_at,
        updated_at=review_session.updated_at,
    )


def _linked_event_summary(
    event: ProviderEventInbox | None,
) -> ReviewRunLinkedEventSummary | None:
    if event is None:
        return None
    return ReviewRunLinkedEventSummary(
        id=event.id,
        provider_event=event.provider_event,
        provider_action=event.provider_action,
        internal_event=event.internal_event,
        delivery_id=event.delivery_id,
        status=event.status,
        error_code=event.error_code,
        error_message=_safe_error_message(event.error_message),
        created_at=event.created_at,
        processed_at=event.processed_at,
    )


def _linked_task_summary(
    task: AgentTask | None,
) -> ReviewRunLinkedTaskSummary | None:
    if task is None:
        return None
    return ReviewRunLinkedTaskSummary(
        id=task.id,
        provider_event_id=task.provider_event_id,
        task_type=task.task_type,
        status=task.status,
        error_message=_safe_error_message(task.error_message),
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _safe_error_message(error_message: str | None) -> str | None:
    if error_message is None:
        return None
    first_line = error_message.splitlines()[0]
    if len(first_line) > 1000:
        return f"{first_line[:1000]}..."
    return first_line


async def cancel_active_agent_tasks_for_pr(
    session: AsyncSession,
    *,
    provider: str,
    repo_full_name: str | None,
    pull_request_number: int | None,
    failure_code: str,
) -> list[AgentTask]:
    if repo_full_name is None or pull_request_number is None:
        return []
    result = await session.execute(
        select(AgentTask).where(
            AgentTask.provider == provider,
            AgentTask.repo_full_name == repo_full_name,
            AgentTask.pull_request_number == pull_request_number,
            AgentTask.task_type == "message_command",
            AgentTask.status.in_({"queued", "running"}),
        )
    )
    tasks = list(result.scalars().all())
    for task in tasks:
        task.status = "running"
        task.stage = "cancellation_pending"
        task.failure_code = failure_code
        task.error_message = "Pull request closed before the command completed."
        task.lock_owner = None
        task.locked_until = None
        session.add(task)
    if tasks:
        await session.commit()
        for task in tasks:
            await session.refresh(task)
    return tasks


async def cancel_active_review_runs_for_pr(
    session: AsyncSession,
    *,
    provider: str,
    repo_full_name: str | None,
    pull_request_number: int | None,
    failure_code: str,
) -> list[ReviewRun]:
    if repo_full_name is None or pull_request_number is None:
        return []

    result = await session.execute(
        select(ReviewRun).where(
            ReviewRun.provider == provider,
            ReviewRun.repo_full_name == repo_full_name,
            ReviewRun.pull_request_number == pull_request_number,
            ReviewRun.status.in_(CANCELLABLE_STATUSES),
        )
    )
    review_runs = list(result.scalars().all())
    now = utc_now()
    for review_run in review_runs:
        review_run.status = "cancelled"
        review_run.stage = "cleanup"
        review_run.failure_code = failure_code
        review_run.completed_at = now
        session.add(review_run)
    return review_runs


async def supersede_older_review_runs(
    session: AsyncSession,
    current_run: ReviewRun,
) -> list[ReviewRun]:
    result = await session.execute(
        select(ReviewRun).where(
            ReviewRun.provider == current_run.provider,
            ReviewRun.repo_full_name == current_run.repo_full_name,
            ReviewRun.pull_request_number == current_run.pull_request_number,
            ReviewRun.id != current_run.id,
            ReviewRun.head_sha != current_run.head_sha,
            ReviewRun.status.in_(CANCELLABLE_STATUSES),
        )
    )
    older_runs = list(result.scalars().all())
    now = utc_now()
    for review_run in older_runs:
        review_run.status = "superseded"
        review_run.stage = "cleanup"
        review_run.failure_code = "superseded_by_new_head"
        review_run.superseded_by_review_run_id = current_run.id
        review_run.completed_at = now
        session.add(review_run)
    return older_runs


async def create_review_run_from_snapshot(
    session: AsyncSession,
    provider: str,
    snapshot: PullRequestSnapshot,
    *,
    trigger_event_id: str | None = None,
) -> ReviewRun:
    return await create_review_run(
        session,
        ReviewRunCreate(
            provider=provider,
            repo_full_name=snapshot.repository,
            pull_request_number=snapshot.number,
            base_sha=snapshot.base_sha,
            head_sha=snapshot.head_sha,
        ),
        trigger_type="webhook",
        trigger_event_id=trigger_event_id,
    )


async def get_or_create_review_config(
    session: AsyncSession,
    *,
    provider: str,
    repo_full_name: str,
    default_skill: str = "code-review",
    default_command_skill: str = "pr-assistant",
) -> ReviewConfig:
    result = await session.execute(
        select(ReviewConfig).where(
            ReviewConfig.provider == provider,
            ReviewConfig.repo_full_name == repo_full_name,
        )
    )
    config = result.scalar_one_or_none()
    if config is not None:
        return config

    config = ReviewConfig(
        provider=provider,
        repo_full_name=repo_full_name,
        default_review_skill=default_skill,
        default_agent_command_skill=default_command_skill,
    )
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return config


def _build_coalesce_key(
    provider: str,
    repo_full_name: str | None,
    pull_request_number: int | None,
    head_sha: str | None,
) -> str | None:
    if repo_full_name is None or pull_request_number is None:
        return None
    if head_sha:
        return f"{provider}:{repo_full_name}:{pull_request_number}:{head_sha}:review"
    return f"{provider}:{repo_full_name}:{pull_request_number}:lifecycle"




async def _mark_failed(
    session: AsyncSession,
    review_run: ReviewRun,
    error: str,
    *,
    failure_code: str = "failed",
) -> ReviewRun:
    review_run.status = "failed"
    review_run.failure_code = failure_code
    review_run.error = error
    review_run.completed_at = utc_now()
    review_session = await _review_session_for_run(session, review_run.id)
    if review_session is not None:
        review_session.status = "failed"
        review_session.error_message = error
        session.add(review_session)
    await session.commit()
    await session.refresh(review_run)
    return review_run


def _finding_count_by_severity(findings: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        severity = str(getattr(finding, "severity", "unknown"))
        counts[severity] = counts.get(severity, 0) + 1
    return counts
