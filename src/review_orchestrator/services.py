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
    ProviderEventInbox,
    PullRequestContext,
    ReviewConfig,
    ReviewRun,
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
    OpenHandsPassthroughStatus,
    OpenHandsSessionDiagnostics,
    ProviderEventInboxDetail,
    ProviderEventInboxListResponse,
    ProviderEventInboxSummary,
    ReviewRunCreate,
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
        review_run.failure_code = "openhands_error"
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
            return await _mark_failed(session, review_run, str(exc))
        if task.status == OpenHandsStartTaskStatus.error:
            return await _mark_failed(
                session,
                review_run,
                task.detail or "OpenHands start task failed.",
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
            return await _mark_failed(session, review_run, str(exc))

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
