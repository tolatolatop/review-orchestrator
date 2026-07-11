from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.github import (
    NormalizedGitHubEvent,
    parse_github_datetime,
    payload_digest,
)
from review_orchestrator.models import (
    AgentTask,
    Finding,
    ProviderEventInbox,
    PullRequestContext,
    ReviewCommentRef,
    ReviewConfig,
    ReviewRun,
    ReviewSession,
    Workspace,
    utc_now,
)
from review_orchestrator.observability import DEFAULT_OBSERVABILITY_SORT, redact_value
from review_orchestrator.openhands import (
    OpenHandsClient,
    OpenHandsClientError,
    OpenHandsStartTaskStatus,
)
from review_orchestrator.providers import ProviderWebhookEvent
from review_orchestrator.reconciliation import persist_and_reconcile_findings
from review_orchestrator.review_results import (
    ChangedFile,
    ParsedReviewResult,
    ReviewResultError,
    ReviewSkillInput,
    parse_review_result,
)
from review_orchestrator.schemas import (
    AgentTaskDetail,
    AgentTaskListResponse,
    AgentTaskQueueHealth,
    AgentTaskSummary,
    OpenHandsPassthroughStatus,
    OpenHandsSessionDiagnostics,
    ProviderEventInboxDetail,
    ProviderEventInboxListResponse,
    ProviderEventInboxSummary,
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
    WebhookAccepted,
)

OPENHANDS_TERMINAL_SUCCESS_STATUSES = {"FINISHED", "COMPLETED", "STOPPED"}
OPENHANDS_TERMINAL_FAILURE_STATUSES = {"ERROR", "STUCK", "FAILED"}
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
    review_run = ReviewRun(
        **values,
        status="queued",
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


async def get_openhands_session_diagnostics_for_review_run(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    openhands_client: OpenHandsClient | None = None,
    openhands_live_status_disabled_reason: str | None = None,
    openhands_ui_base_url: str | None = None,
) -> OpenHandsSessionDiagnostics:
    agent_task_ids = await _get_agent_task_ids_for_review_run(session, review_run)
    execution_status: str | None = None
    sandbox_status: str | None = None
    live_status_error = openhands_live_status_disabled_reason

    if review_run.openhands_conversation_id and openhands_client is not None:
        try:
            conversation = await openhands_client.get_conversation(
                review_run.openhands_conversation_id
            )
        except OpenHandsClientError as exc:
            live_status_error = str(exc)
        else:
            execution_status = conversation.execution_status
            sandbox_status = conversation.sandbox_status
            live_status_error = None

    return OpenHandsSessionDiagnostics(
        review_run_id=review_run.id,
        agent_task_ids=agent_task_ids,
        provider=review_run.provider,
        repo_full_name=review_run.repo_full_name,
        pull_request_number=review_run.pull_request_number,
        status=review_run.status,
        stage=review_run.stage,
        openhands_start_task_id=review_run.openhands_start_task_id,
        openhands_conversation_id=review_run.openhands_conversation_id,
        openhands_sandbox_id=review_run.openhands_sandbox_id,
        openhands_agent_server_url=review_run.openhands_agent_server_url,
        execution_status=execution_status,
        sandbox_status=sandbox_status,
        session_available=bool(review_run.openhands_conversation_id),
        live_status_available=(
            execution_status is not None or sandbox_status is not None
        ),
        live_status_error=live_status_error,
        passthrough=_build_openhands_passthrough_status(
            conversation_id=review_run.openhands_conversation_id,
            openhands_ui_base_url=openhands_ui_base_url,
        ),
        created_at=review_run.created_at,
        updated_at=review_run.updated_at,
    )


async def get_openhands_session_diagnostics_for_conversation(
    session: AsyncSession,
    conversation_id: str,
    *,
    openhands_client: OpenHandsClient | None = None,
    openhands_live_status_disabled_reason: str | None = None,
    openhands_ui_base_url: str | None = None,
) -> OpenHandsSessionDiagnostics | None:
    result = await session.execute(
        select(ReviewRun)
        .where(ReviewRun.openhands_conversation_id == conversation_id)
        .order_by(ReviewRun.created_at.desc(), ReviewRun.id.desc())
        .limit(1)
    )
    review_run = result.scalar_one_or_none()
    if review_run is None:
        return None
    return await get_openhands_session_diagnostics_for_review_run(
        session,
        review_run,
        openhands_client=openhands_client,
        openhands_live_status_disabled_reason=openhands_live_status_disabled_reason,
        openhands_ui_base_url=openhands_ui_base_url,
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


def _build_openhands_passthrough_status(
    *,
    conversation_id: str | None,
    openhands_ui_base_url: str | None,
) -> OpenHandsPassthroughStatus:
    if not conversation_id:
        return OpenHandsPassthroughStatus(
            enabled=False,
            reason="OpenHands conversation id is not recorded for this review run.",
        )
    if not openhands_ui_base_url:
        return OpenHandsPassthroughStatus(
            enabled=False,
            reason="OPENHANDS_UI_BASE_URL is not configured.",
        )
    return OpenHandsPassthroughStatus(
        enabled=True,
        conversation_url=(
            f"{openhands_ui_base_url.rstrip('/')}/conversations/{conversation_id}"
        ),
    )


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
    return ReviewRunListResponse(
        items=[
            _review_run_list_item(
                review_run,
                publishing_by_run_id.get(review_run.id),
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
    list_item = _review_run_list_item(review_run, publishing)

    return ReviewRunDetail(
        **list_item.model_dump(),
        pull_request_context=_pull_request_context_summary(context),
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
    openhands_client: OpenHandsClient,
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
    try:
        task = await openhands_client.start_conversation(review_input)
    except OpenHandsClientError as exc:
        review_run.status = "failed"
        review_run.failure_code = (
            "openhands_infrastructure_error"
            if _is_openhands_infrastructure_error(str(exc))
            else "openhands_error"
        )
        review_run.error = str(exc)
        await session.commit()
        await session.refresh(review_run)
        return review_run

    review_run.status = "running"
    review_run.started_at = utc_now()
    review_run.workspace_path = resolved_workspace_path
    review_run.openhands_start_task_id = task.id
    review_run.openhands_conversation_id = task.app_conversation_id
    review_run.openhands_sandbox_id = task.sandbox_id
    review_run.openhands_agent_server_url = task.agent_server_url
    review_run.failure_code = None
    review_run.error = None
    await session.commit()
    await session.refresh(review_run)
    return review_run


async def sync_review_session(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    openhands_client: OpenHandsClient,
) -> ReviewRun:
    if review_run.status in {"cancelled", "superseded", "completed", "failed"}:
        return review_run

    if review_run.openhands_start_task_id and not review_run.openhands_conversation_id:
        try:
            task = await openhands_client.get_start_task(
                review_run.openhands_start_task_id
            )
        except OpenHandsClientError as exc:
            return await _mark_failed(
                session,
                review_run,
                str(exc),
                failure_code="openhands_infrastructure_error",
            )
        if task.status == OpenHandsStartTaskStatus.error:
            return await _mark_failed(
                session,
                review_run,
                task.detail or "OpenHands start task failed.",
                failure_code=(
                    "openhands_infrastructure_error"
                    if _is_openhands_infrastructure_error(task.detail)
                    else "openhands_error"
                ),
            )
        if task.status == OpenHandsStartTaskStatus.ready:
            review_run.openhands_conversation_id = task.app_conversation_id
            review_run.openhands_sandbox_id = task.sandbox_id
            review_run.openhands_agent_server_url = task.agent_server_url
            review_run.status = "running"

    if review_run.openhands_conversation_id:
        try:
            conversation = await openhands_client.get_conversation(
                review_run.openhands_conversation_id
            )
        except OpenHandsClientError as exc:
            return await _mark_failed(
                session,
                review_run,
                str(exc),
                failure_code="openhands_infrastructure_error",
            )

        if conversation.sandbox_status in {"ERROR", "MISSING"}:
            return await _mark_failed(
                session,
                review_run,
                f"OpenHands sandbox is {conversation.sandbox_status}.",
            )
        execution_status = (conversation.execution_status or "").upper()
        if execution_status in OPENHANDS_TERMINAL_FAILURE_STATUSES:
            return await _mark_failed(
                session,
                review_run,
                f"OpenHands conversation ended with {execution_status}.",
                failure_code="openhands_error",
            )
        if execution_status in OPENHANDS_TERMINAL_SUCCESS_STATUSES:
            review_run.status = "running"

    await session.commit()
    await session.refresh(review_run)
    return review_run


async def cancel_review_session(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    openhands_client: OpenHandsClient,
    reason: str,
) -> ReviewRun:
    if review_run.status in {"completed", "cancelled", "superseded"}:
        return review_run

    if review_run.openhands_conversation_id:
        try:
            await openhands_client.delete_conversation(
                review_run.openhands_conversation_id
            )
        except OpenHandsClientError as exc:
            review_run.error = f"Cancel requested; OpenHands cleanup failed: {exc}"
        else:
            review_run.error = reason
    else:
        review_run.error = reason

    review_run.status = "cancelled"
    await session.commit()
    await session.refresh(review_run)
    return review_run


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
    review_run.review_summary = parsed.result.summary
    review_run.failure_code = None
    review_run.error = None
    review_run.completed_at = utc_now()
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
        context = await upsert_pull_request_context(session, event, payload)

    if normalized_event.internal_event in {"pr_closed", "pr_merged"}:
        await cancel_active_review_runs_for_pr(
            session,
            provider=provider,
            repo_full_name=event.repo_full_name,
            pull_request_number=event.pull_request_number,
            failure_code=normalized_event.internal_event,
        )

    if normalized_event.should_create_review_run:
        review_run = await create_review_run_from_provider_payload(
            session,
            event.provider,
            payload,
            trigger_event_id=event.id,
        )
        await supersede_older_review_runs(session, review_run)
        review_run_id = review_run.id
        event.review_run_id = review_run_id
        event.status = "queued"
        if context is not None:
            context.latest_review_run_id = review_run_id

    if normalized_event.should_create_agent_task:
        agent_task = await create_agent_task_from_event(
            session,
            event,
            payload=payload,
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


async def accept_github_webhook(
    session: AsyncSession,
    *,
    delivery_id: str,
    provider_event: str,
    normalized_event: NormalizedGitHubEvent,
    payload: dict[str, Any],
    raw_body: bytes,
) -> WebhookAccepted:
    return await accept_provider_webhook(
        session,
        delivery_id=delivery_id,
        provider_event=provider_event,
        normalized_event=normalized_event.to_provider_event(),
        payload=payload,
        raw_body=raw_body,
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
    payload: dict[str, Any],
) -> PullRequestContext | None:
    identity = _pull_request_identity(event.provider, payload)
    if identity is None:
        return None

    repository_name = identity["repository"]
    pull_request_number = identity["number"]
    if not repository_name or not isinstance(pull_request_number, int):
        return None

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
            head_sha=identity["head_sha"] or "",
        )
        session.add(context)

    context.provider_repo_id = identity["provider_repo_id"]
    context.provider_pr_id = identity["provider_pr_id"]
    context.title = identity["title"]
    context.author_login = identity["author_login"]
    context.base_ref = identity["base_ref"]
    context.base_sha = identity["base_sha"]
    context.head_ref = identity["head_ref"]
    context.head_sha = identity["head_sha"] or context.head_sha
    context.head_repo_full_name = identity["head_repo_full_name"]
    context.is_fork = bool(
        context.head_repo_full_name
        and context.head_repo_full_name != identity["base_repo_full_name"]
    )
    context.status = identity["status"]
    context.html_url = identity["html_url"]
    context.latest_event_id = event.id
    context.closed_at = identity["closed_at"]
    context.merged_at = identity["merged_at"]

    return context


async def create_agent_task_from_event(
    session: AsyncSession,
    event: ProviderEventInbox,
    *,
    payload: dict[str, Any],
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
        provider_event_id=event.id,
        pull_request_context_id=context.id if context else None,
        provider=event.provider,
        repo_full_name=event.repo_full_name,
        pull_request_number=event.pull_request_number,
        task_type="mention",
        status="queued",
        input_json={
            "internal_event": event.internal_event,
            "provider_action": event.provider_action,
            "payload": payload,
        },
    )
    session.add(agent_task)
    await session.flush()
    return agent_task


async def list_agent_tasks(
    session: AsyncSession,
    *,
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
        items=[_agent_task_summary(task) for task in tasks],
        total=total,
        limit=limit,
        offset=offset,
        queue=await _agent_task_queue_health(session, health_filters),
    )


async def get_agent_task_detail(
    session: AsyncSession,
    task_id: str,
) -> AgentTaskDetail | None:
    task = await session.get(AgentTask, task_id)
    if task is None:
        return None
    return AgentTaskDetail(
        **_agent_task_summary(task).model_dump(),
        input_metadata=redact_value(task.input_json),
        result_json=task.result_json,
    )


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
        oldest_queued_age_seconds=oldest_queued_age_seconds,
    )


def _agent_task_summary(task: AgentTask) -> AgentTaskSummary:
    return AgentTaskSummary(
        id=task.id,
        provider=task.provider,
        repo_full_name=task.repo_full_name,
        pull_request_number=task.pull_request_number,
        task_type=task.task_type,
        status=task.status,
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
) -> ReviewRunListItem:
    base = ReviewRunRead.model_validate(review_run).model_dump()
    base["error"] = _safe_error_message(review_run.error)
    return ReviewRunListItem(
        **base,
        operational_state=_operational_state(review_run),
        provider_publishing=publishing or ReviewRunProviderPublishing(
            summary_comment_id=review_run.summary_comment_id,
            summary_published=review_run.summary_comment_id is not None,
        ),
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
    if review_run.status == "running" and not review_run.openhands_conversation_id:
        return "waiting_for_openhands"
    if review_run.status == "running":
        return "running_in_openhands"
    return review_run.status


async def _provider_publishing_for_runs(
    session: AsyncSession,
    review_run_ids: list[str],
) -> dict[str, ReviewRunProviderPublishing]:
    if not review_run_ids:
        return {}

    result = await session.execute(
        select(ReviewCommentRef).where(ReviewCommentRef.review_run_id.in_(review_run_ids))
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
    return (
        await _provider_publishing_for_runs(session, [review_run_id])
    ).get(review_run_id, ReviewRunProviderPublishing())


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
        openhands_conversation_id=review_session.openhands_conversation_id,
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


async def create_review_run_from_github_payload(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    trigger_event_id: str | None = None,
) -> ReviewRun:
    pull_request = payload.get("pull_request")
    repository = payload.get("repository")
    if not isinstance(pull_request, dict) or not isinstance(repository, dict):
        raise ValueError("GitHub pull_request payload is missing required objects.")

    repository_name = _str_or_none(repository.get("full_name"))
    pull_request_number = pull_request.get("number")
    head_sha = _head_sha(pull_request)
    if not repository_name or not isinstance(pull_request_number, int) or not head_sha:
        raise ValueError("GitHub pull_request payload is missing PR identity fields.")

    return await create_review_run(
        session,
        ReviewRunCreate(
            provider="github",
            repo_full_name=repository_name,
            pull_request_number=pull_request_number,
            base_sha=_base_sha(pull_request),
            head_sha=head_sha,
        ),
        trigger_type="webhook",
        trigger_event_id=trigger_event_id,
    )


async def create_review_run_from_provider_payload(
    session: AsyncSession,
    provider: str,
    payload: dict[str, Any],
    *,
    trigger_event_id: str | None = None,
) -> ReviewRun:
    if provider == "github":
        return await create_review_run_from_github_payload(
            session, payload, trigger_event_id=trigger_event_id
        )

    identity = _pull_request_identity(provider, payload)
    if identity is None:
        raise ValueError(f"{provider} payload is missing PR identity fields.")
    return await create_review_run(
        session,
        ReviewRunCreate(
            provider=provider,
            repo_full_name=identity["repository"],
            pull_request_number=identity["number"],
            base_sha=identity["base_sha"],
            head_sha=identity["head_sha"],
        ),
        trigger_type="webhook",
        trigger_event_id=trigger_event_id,
    )


async def get_or_create_review_config(
    session: AsyncSession,
    *,
    provider: str,
    repo_full_name: str,
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

    config = ReviewConfig(provider=provider, repo_full_name=repo_full_name)
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


def _pull_request_identity(
    provider: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if provider == "github":
        pull_request = payload.get("pull_request")
        repository = payload.get("repository")
        if not isinstance(pull_request, dict) or not isinstance(repository, dict):
            return None
        repository_name = _str_or_none(repository.get("full_name"))
        pull_request_number = pull_request.get("number")
        if not repository_name or not isinstance(pull_request_number, int):
            return None
        base = pull_request.get("base")
        head = pull_request.get("head")
        base_repo = base.get("repo") if isinstance(base, dict) else None
        head_repo = head.get("repo") if isinstance(head, dict) else None
        return {
            "repository": repository_name,
            "number": pull_request_number,
            "provider_repo_id": _id_to_str(repository.get("id")),
            "provider_pr_id": _id_to_str(pull_request.get("id")),
            "title": _str_or_none(pull_request.get("title")),
            "author_login": _login(pull_request.get("user")),
            "base_ref": _ref(base),
            "base_sha": _sha(base),
            "head_ref": _ref(head),
            "head_sha": _head_sha(pull_request),
            "base_repo_full_name": _repo_full_name(base_repo),
            "head_repo_full_name": _repo_full_name(head_repo),
            "status": _pull_request_status(pull_request),
            "html_url": _str_or_none(pull_request.get("html_url")),
            "closed_at": parse_github_datetime(pull_request.get("closed_at")),
            "merged_at": parse_github_datetime(pull_request.get("merged_at")),
        }

    if provider == "gitlab":
        attrs = payload.get("object_attributes")
        project = payload.get("project")
        if not isinstance(attrs, dict) or not isinstance(project, dict):
            return None
        repository_name = _str_or_none(project.get("path_with_namespace"))
        pull_request_number = attrs.get("iid")
        head_sha = None
        last_commit = attrs.get("last_commit")
        if isinstance(last_commit, dict):
            head_sha = _str_or_none(last_commit.get("id"))
        head_sha = head_sha or _str_or_none(attrs.get("last_commit_id"))
        if (
            not repository_name
            or not isinstance(pull_request_number, int)
            or not head_sha
        ):
            return None
        target = attrs.get("target")
        source = attrs.get("source")
        return {
            "repository": repository_name,
            "number": pull_request_number,
            "provider_repo_id": _id_to_str(project.get("id")),
            "provider_pr_id": _id_to_str(attrs.get("id")),
            "title": _str_or_none(attrs.get("title")),
            "author_login": _gitlab_username(payload.get("user")),
            "base_ref": _str_or_none(attrs.get("target_branch")),
            "base_sha": _str_or_none(attrs.get("target_branch_sha")),
            "head_ref": _str_or_none(attrs.get("source_branch")),
            "head_sha": head_sha,
            "base_repo_full_name": _gitlab_project_path(target),
            "head_repo_full_name": _gitlab_project_path(source),
            "status": _str_or_none(attrs.get("state")) or "open",
            "html_url": _str_or_none(attrs.get("url")),
            "closed_at": parse_github_datetime(attrs.get("closed_at")),
            "merged_at": parse_github_datetime(attrs.get("merged_at")),
        }

    return None


def _pull_request_status(pull_request: dict[str, Any]) -> str:
    if pull_request.get("merged") is True:
        return "merged"
    state = pull_request.get("state")
    return state if isinstance(state, str) and state else "open"


def _base_sha(pull_request: dict[str, Any]) -> str | None:
    base = pull_request.get("base")
    return _sha(base)


def _head_sha(pull_request: dict[str, Any]) -> str | None:
    head = pull_request.get("head")
    return _sha(head)


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


def _gitlab_project_path(project: Any) -> str | None:
    if not isinstance(project, dict):
        return None
    return _str_or_none(project.get("path_with_namespace"))


def _gitlab_username(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return _str_or_none(user.get("username")) or _str_or_none(user.get("name"))


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
    await session.commit()
    await session.refresh(review_run)
    return review_run


def _is_openhands_infrastructure_error(detail: str | None) -> bool:
    if not detail:
        return False
    normalized = detail.lower()
    return any(
        marker in normalized
        for marker in (
            "coroutine raised stopiteration",
            "sandbox server not running",
            "failed to start container",
            "port is already allocated",
            "openhands request failed",
        )
    )


def _finding_count_by_severity(findings: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        severity = str(getattr(finding, "severity", "unknown"))
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def _login(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return _str_or_none(user.get("login"))


def _id_to_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
