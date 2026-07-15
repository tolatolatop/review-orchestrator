"""FastAPI routes and transport-level error mapping."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.application.services import (
    ReviewRequestRejected,
    ReviewRunTransitionError,
    accept_provider_webhook,
    cancel_agent_task,
    cancel_review_run,
    cancel_review_session,
    collect_review_result,
    get_agent_task_detail,
    get_delivery_outbox,
    get_pi_agent_session_diagnostics_for_agent_task,
    get_pi_agent_session_diagnostics_for_review_run,
    get_pi_agent_session_diagnostics_for_session,
    get_provider_event_inbox_detail,
    get_review_run,
    get_review_run_detail,
    get_session_archive,
    get_task_summary,
    list_agent_tasks,
    list_delivery_outbox,
    list_provider_event_inbox,
    list_resource_pools,
    list_review_runs,
    list_task_session_archives,
    list_tasks,
    request_review_rerun,
    request_review_retry,
    retry_agent_task,
    set_resource_pool_capacity,
    start_review_session,
    sync_review_session,
    update_delivery_scheduling,
    update_task_scheduling,
)
from review_orchestrator.domain.models import AgentTask
from review_orchestrator.domain.review_results import ReviewResultError
from review_orchestrator.domain.schemas import (
    AgentTaskDetail,
    AgentTaskListResponse,
    CleanupSummary,
    DeliveryOutboxListResponse,
    DeliveryOutboxSummary,
    DeliverySchedulingUpdate,
    PiAgentSessionDiagnostics,
    PlatformPermissionDiagnosticRequest,
    PlatformPermissionDiagnosticResponse,
    ProviderEventInboxDetail,
    ProviderEventInboxListResponse,
    ProviderInfo,
    ProviderListResponse,
    PullRequestWorkspaceCleanupRequest,
    ResourcePoolListResponse,
    ResourcePoolRead,
    ResourcePoolUpdate,
    ReviewResultCollect,
    ReviewResultCollectResponse,
    ReviewRunActionResult,
    ReviewRunDetail,
    ReviewRunListResponse,
    ReviewRunRead,
    ReviewRunRerunRequest,
    ReviewRunRerunResult,
    ReviewRunRetryResult,
    ReviewSessionCancel,
    ReviewSessionStart,
    SessionArchiveListResponse,
    SessionArchiveRead,
    TaskListResponse,
    TaskSchedulingUpdate,
    TaskSummary,
    WebhookAccepted,
    WorkspaceCleanupRequest,
    WorkspaceLeaseRead,
    WorkspaceLeaseRequest,
    WorkspacePrepareRequest,
    WorkspacePrepareResponse,
    WorkspaceRead,
)
from review_orchestrator.infrastructure.db import get_session
from review_orchestrator.infrastructure.workspaces import (
    cleanup_expired_workspaces,
    cleanup_pull_request_workspaces,
    cleanup_workspace,
    get_workspace,
    lease_workspace,
    prepare_workspace,
    release_workspace,
)
from review_orchestrator.integrations.pi_agent import PiAgentClient
from review_orchestrator.integrations.platform_diagnostics import (
    diagnose_platform_permissions,
)
from review_orchestrator.integrations.providers import (
    ProviderCapabilityError,
    ProviderRegistry,
    ProviderWebhookError,
    WebhookCapability,
)

router = APIRouter(prefix="/api/v1")
session_dependency = Depends(get_session)


def get_configured_provider_registry(request: Request) -> ProviderRegistry:
    injected = getattr(request.app.state, "provider_registry", None)
    if injected is not None:
        return injected
    raise RuntimeError("Provider registry was not initialized by the application.")


def _review_request_rejection_detail(exc: ReviewRequestRejected) -> dict:
    return {
        "code": exc.code,
        "message": exc.message,
        "review_request_event_id": exc.event_id,
        "existing_review_run_id": exc.existing_review_run_id,
    }


@router.post(
    "/diagnostics/platform-permissions",
    response_model=PlatformPermissionDiagnosticResponse,
)
async def diagnose_platform_permissions_endpoint(
    payload: PlatformPermissionDiagnosticRequest,
    request: Request,
) -> PlatformPermissionDiagnosticResponse:
    injected = getattr(request.app.state, "platform_permission_probe", None)
    if injected is not None:
        return await injected(request.app.state.settings, payload)
    registry = get_configured_provider_registry(request)
    try:
        return await diagnose_platform_permissions(
            request.app.state.settings,
            payload,
            provider_registry=registry,
        )
    except ProviderCapabilityError as exc:
        status_code = (
            status.HTTP_404_NOT_FOUND
            if registry.get(payload.provider) is None
            else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.get("/providers", response_model=ProviderListResponse)
async def list_providers_endpoint(request: Request) -> ProviderListResponse:
    registry = get_configured_provider_registry(request)
    return ProviderListResponse(
        items=[
            ProviderInfo(
                key=descriptor.key,
                kind=descriptor.kind,
                display_name=descriptor.display_name,
                capabilities=sorted(registry.capabilities(descriptor.key)),
            )
            for descriptor in registry.descriptors()
        ]
    )


def get_pi_agent_client(request: Request) -> PiAgentClient:
    injected = getattr(request.app.state, "pi_agent_client", None)
    if injected is not None:
        return injected

    settings = request.app.state.settings
    if not settings.pi_agent_base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pi-agent runtime base URL is not configured.",
        )
    return PiAgentClient(
        base_url=settings.pi_agent_base_url,
        api_token=settings.pi_agent_runtime_token,
        timeout=settings.pi_agent_timeout_seconds,
    )


pi_agent_client_dependency = Depends(get_pi_agent_client)


def get_optional_pi_agent_client(
    request: Request,
) -> tuple[PiAgentClient | None, str | None]:
    injected = getattr(request.app.state, "pi_agent_client", None)
    if injected is not None:
        return injected, None

    settings = request.app.state.settings
    if not settings.pi_agent_base_url:
        return None, "pi-agent runtime base URL is not configured."
    return (
        PiAgentClient(
            base_url=settings.pi_agent_base_url,
            api_token=settings.pi_agent_runtime_token,
            timeout=settings.pi_agent_timeout_seconds,
        ),
        None,
    )


@router.post("/webhooks/{provider}", response_model=WebhookAccepted)
async def accept_webhook(
    provider: str,
    request: Request,
    session: AsyncSession = session_dependency,
) -> WebhookAccepted:
    registry = get_configured_provider_registry(request)
    adapter = registry.capability(provider, WebhookCapability)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unsupported provider: {provider}",
        )

    raw_body = await request.body()
    try:
        parsed = adapter.parse_webhook(
            headers=dict(request.headers),
            raw_body=raw_body,
            settings=request.app.state.settings,
        )
    except ProviderWebhookError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return await accept_provider_webhook(
        session,
        delivery_id=parsed.delivery_id,
        provider_event=parsed.provider_event.provider_event,
        normalized_event=parsed.provider_event,
        payload=parsed.payload,
        raw_body=parsed.raw_body,
    )


@router.get("/provider-events", response_model=ProviderEventInboxListResponse)
@router.get(
    "/observability/provider-events",
    response_model=ProviderEventInboxListResponse,
)
async def list_provider_events_endpoint(
    provider: str | None = Query(default=None, min_length=1, max_length=64),
    repo_full_name: str | None = Query(default=None, min_length=1, max_length=512),
    pull_request_number: int | None = Query(default=None, gt=0),
    internal_event: str | None = Query(default=None, min_length=1, max_length=128),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        min_length=1,
        max_length=32,
    ),
    delivery_id: str | None = Query(default=None, min_length=1, max_length=128),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = session_dependency,
) -> ProviderEventInboxListResponse:
    return await list_provider_event_inbox(
        session,
        provider=provider,
        repo_full_name=repo_full_name,
        pull_request_number=pull_request_number,
        internal_event=internal_event,
        status=status_filter,
        delivery_id=delivery_id,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )


@router.get("/provider-events/{event_id}", response_model=ProviderEventInboxDetail)
@router.get(
    "/observability/provider-events/{event_id}",
    response_model=ProviderEventInboxDetail,
)
async def get_provider_event_endpoint(
    event_id: str,
    include_payload: bool = False,
    session: AsyncSession = session_dependency,
) -> ProviderEventInboxDetail:
    event = await get_provider_event_inbox_detail(
        session,
        event_id,
        include_payload=include_payload,
    )
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return event


@router.get("/tasks", response_model=TaskListResponse)
@router.get("/observability/tasks", response_model=TaskListResponse)
async def list_tasks_endpoint(
    kind: str | None = Query(default=None, min_length=1, max_length=32),
    capability_id: str | None = Query(default=None, min_length=1, max_length=128),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        min_length=1,
        max_length=32,
    ),
    queue: str | None = Query(default=None, min_length=1, max_length=64),
    resource_class: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = session_dependency,
) -> TaskListResponse:
    return await list_tasks(
        session,
        kind=kind,
        capability_id=capability_id,
        status=status_filter,
        queue=queue,
        resource_class=resource_class,
        limit=limit,
        offset=offset,
    )


@router.get("/tasks/{task_id}", response_model=TaskSummary)
@router.get("/observability/tasks/{task_id}", response_model=TaskSummary)
async def get_task_endpoint(
    task_id: str,
    session: AsyncSession = session_dependency,
) -> TaskSummary:
    task = await get_task_summary(session, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return task


@router.get(
    "/tasks/{task_id}/sessions",
    response_model=SessionArchiveListResponse,
)
@router.get(
    "/observability/tasks/{task_id}/sessions",
    response_model=SessionArchiveListResponse,
)
async def list_task_sessions_endpoint(
    task_id: str,
    session: AsyncSession = session_dependency,
) -> SessionArchiveListResponse:
    archives = await list_task_session_archives(session, task_id)
    if archives is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return archives


@router.get(
    "/session-archives/{archive_id}",
    response_model=SessionArchiveRead,
)
@router.get(
    "/observability/session-archives/{archive_id}",
    response_model=SessionArchiveRead,
)
async def get_session_archive_endpoint(
    archive_id: str,
    session: AsyncSession = session_dependency,
) -> SessionArchiveRead:
    archive = await get_session_archive(session, archive_id)
    if archive is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return archive


@router.patch("/tasks/{task_id}/scheduling", response_model=TaskSummary)
async def update_task_scheduling_endpoint(
    task_id: str,
    payload: TaskSchedulingUpdate,
    session: AsyncSession = session_dependency,
) -> TaskSummary:
    try:
        task = await update_task_scheduling(session, task_id, payload)
    except ReviewRunTransitionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return task


@router.get("/resource-pools", response_model=ResourcePoolListResponse)
@router.get(
    "/observability/resource-pools",
    response_model=ResourcePoolListResponse,
)
async def list_resource_pools_endpoint(
    session: AsyncSession = session_dependency,
) -> ResourcePoolListResponse:
    return await list_resource_pools(session)


@router.put("/resource-pools/{resource_key:path}", response_model=ResourcePoolRead)
async def update_resource_pool_endpoint(
    resource_key: str,
    payload: ResourcePoolUpdate,
    session: AsyncSession = session_dependency,
) -> ResourcePoolRead:
    try:
        pool = await set_resource_pool_capacity(
            session,
            resource_key,
            capacity=payload.capacity,
            dimension=payload.dimension,
        )
    except ReviewRunTransitionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return pool


@router.get("/deliveries", response_model=DeliveryOutboxListResponse)
@router.get(
    "/observability/deliveries",
    response_model=DeliveryOutboxListResponse,
)
async def list_deliveries_endpoint(
    task_id: str | None = Query(default=None, min_length=1, max_length=36),
    provider: str | None = Query(default=None, min_length=1, max_length=64),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        min_length=1,
        max_length=32,
    ),
    queue: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = session_dependency,
) -> DeliveryOutboxListResponse:
    return await list_delivery_outbox(
        session,
        task_id=task_id,
        provider=provider,
        status=status_filter,
        queue=queue,
        limit=limit,
        offset=offset,
    )


@router.get("/deliveries/{delivery_id}", response_model=DeliveryOutboxSummary)
@router.get(
    "/observability/deliveries/{delivery_id}",
    response_model=DeliveryOutboxSummary,
)
async def get_delivery_endpoint(
    delivery_id: str,
    session: AsyncSession = session_dependency,
) -> DeliveryOutboxSummary:
    delivery = await get_delivery_outbox(session, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return delivery


@router.patch(
    "/deliveries/{delivery_id}/scheduling",
    response_model=DeliveryOutboxSummary,
)
async def update_delivery_scheduling_endpoint(
    delivery_id: str,
    payload: DeliverySchedulingUpdate,
    session: AsyncSession = session_dependency,
) -> DeliveryOutboxSummary:
    try:
        delivery = await update_delivery_scheduling(session, delivery_id, payload)
    except ReviewRunTransitionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if delivery is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return delivery


@router.get("/agent-tasks", response_model=AgentTaskListResponse)
@router.get("/observability/agent-tasks", response_model=AgentTaskListResponse)
async def list_agent_tasks_endpoint(
    request: Request,
    status_filter: str | None = Query(
        default=None,
        alias="status",
        min_length=1,
        max_length=32,
    ),
    provider: str | None = Query(default=None, min_length=1, max_length=64),
    repo_full_name: str | None = Query(default=None, min_length=1, max_length=512),
    pull_request_number: int | None = Query(default=None, gt=0),
    task_type: str | None = Query(default=None, min_length=1, max_length=64),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = session_dependency,
) -> AgentTaskListResponse:
    return await list_agent_tasks(
        session,
        provider_registry=get_configured_provider_registry(request),
        status=status_filter,
        provider=provider,
        repo_full_name=repo_full_name,
        pull_request_number=pull_request_number,
        task_type=task_type,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )


@router.get("/agent-tasks/{task_id}", response_model=AgentTaskDetail)
@router.get("/observability/agent-tasks/{task_id}", response_model=AgentTaskDetail)
async def get_agent_task_endpoint(
    task_id: str,
    request: Request,
    session: AsyncSession = session_dependency,
) -> AgentTaskDetail:
    task = await get_agent_task_detail(
        session,
        task_id,
        provider_registry=get_configured_provider_registry(request),
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return task


@router.get(
    "/agent-tasks/{task_id}/agent-session",
    response_model=PiAgentSessionDiagnostics,
)
async def get_agent_task_session_endpoint(
    task_id: str,
    session: AsyncSession = session_dependency,
    pi_agent_client: PiAgentClient = pi_agent_client_dependency,
) -> PiAgentSessionDiagnostics:
    task = await session.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return await get_pi_agent_session_diagnostics_for_agent_task(
        task,
        pi_agent_client=pi_agent_client,
    )


@router.post(
    "/agent-tasks/{task_id}/cancel",
    response_model=AgentTaskDetail,
)
async def cancel_agent_task_endpoint(
    task_id: str,
    request: Request,
    session: AsyncSession = session_dependency,
    pi_agent_client: PiAgentClient = pi_agent_client_dependency,
) -> AgentTaskDetail:
    registry = get_configured_provider_registry(request)
    try:
        task = await cancel_agent_task(
            session,
            task_id,
            pi_agent_client=pi_agent_client,
            provider_registry=registry,
        )
    except ReviewRunTransitionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    detail = await get_agent_task_detail(
        session,
        task.id,
        provider_registry=registry,
    )
    assert detail is not None
    return detail


@router.post(
    "/agent-tasks/{task_id}/retry",
    response_model=AgentTaskDetail,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_agent_task_endpoint(
    task_id: str,
    request: Request,
    session: AsyncSession = session_dependency,
) -> AgentTaskDetail:
    try:
        task = await retry_agent_task(session, task_id)
    except ReviewRunTransitionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    detail = await get_agent_task_detail(
        session,
        task.id,
        provider_registry=get_configured_provider_registry(request),
    )
    assert detail is not None
    return detail


@router.get("/review-runs", response_model=ReviewRunListResponse)
@router.get("/observability/review-runs", response_model=ReviewRunListResponse)
async def list_review_runs_endpoint(
    provider: str | None = Query(default=None, min_length=1, max_length=64),
    repo_full_name: str | None = Query(default=None, min_length=1, max_length=512),
    pull_request_number: int | None = Query(default=None, gt=0),
    merge_request_number: int | None = Query(default=None, gt=0),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        min_length=1,
        max_length=32,
    ),
    stage: str | None = Query(default=None, min_length=1, max_length=64),
    head_sha: str | None = Query(default=None, min_length=7, max_length=80),
    trigger_type: str | None = Query(default=None, min_length=1, max_length=32),
    lock_state: str | None = Query(
        default=None,
        pattern="^(locked|unlocked|expired)$",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = session_dependency,
) -> ReviewRunListResponse:
    return await list_review_runs(
        session,
        provider=provider,
        repo_full_name=repo_full_name,
        pull_request_number=pull_request_number,
        merge_request_number=merge_request_number,
        status=status_filter,
        stage=stage,
        head_sha=head_sha,
        trigger_type=trigger_type,
        lock_state=lock_state,
        limit=limit,
        offset=offset,
    )


@router.get("/review-runs/{review_run_id}", response_model=ReviewRunDetail)
@router.get(
    "/observability/review-runs/{review_run_id}",
    response_model=ReviewRunDetail,
)
async def get_review_run_endpoint(
    review_run_id: str,
    session: AsyncSession = session_dependency,
) -> ReviewRunDetail:
    review_run = await get_review_run_detail(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return review_run


@router.get(
    "/observability/review-runs/{review_run_id}/agent-session",
    response_model=PiAgentSessionDiagnostics,
)
async def get_review_run_agent_session_endpoint(
    review_run_id: str,
    request: Request,
    session: AsyncSession = session_dependency,
) -> PiAgentSessionDiagnostics:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    pi_agent_client, disabled_reason = get_optional_pi_agent_client(request)
    return await get_pi_agent_session_diagnostics_for_review_run(
        session,
        review_run,
        pi_agent_client=pi_agent_client,
        live_status_disabled_reason=disabled_reason,
    )


@router.get(
    "/observability/agent-sessions/{agent_session_id}",
    response_model=PiAgentSessionDiagnostics,
)
async def get_pi_agent_session_endpoint(
    agent_session_id: str,
    request: Request,
    session: AsyncSession = session_dependency,
) -> PiAgentSessionDiagnostics:
    pi_agent_client, disabled_reason = get_optional_pi_agent_client(request)
    diagnostics = await get_pi_agent_session_diagnostics_for_session(
        session,
        agent_session_id,
        pi_agent_client=pi_agent_client,
        live_status_disabled_reason=disabled_reason,
    )
    if diagnostics is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return diagnostics


@router.post("/review-runs/{review_run_id}/session/start", response_model=ReviewRunRead)
async def start_review_session_endpoint(
    review_run_id: str,
    payload: ReviewSessionStart,
    request: Request,
    session: AsyncSession = session_dependency,
    pi_agent_client: PiAgentClient = pi_agent_client_dependency,
) -> ReviewRunRead:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        return await start_review_session(
            session,
            review_run,
            pi_agent_client=pi_agent_client,
            settings=request.app.state.settings,
            workspace_path=payload.workspace_path,
        )
    except ReviewRunTransitionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/review-runs/{review_run_id}/session/sync", response_model=ReviewRunRead)
async def sync_review_session_endpoint(
    review_run_id: str,
    session: AsyncSession = session_dependency,
    pi_agent_client: PiAgentClient = pi_agent_client_dependency,
) -> ReviewRunRead:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return await sync_review_session(
        session,
        review_run,
        pi_agent_client=pi_agent_client,
    )


@router.post(
    "/review-runs/{review_run_id}/session/cancel",
    response_model=ReviewRunRead,
)
async def cancel_review_session_endpoint(
    review_run_id: str,
    payload: ReviewSessionCancel,
    session: AsyncSession = session_dependency,
    pi_agent_client: PiAgentClient = pi_agent_client_dependency,
) -> ReviewRunRead:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return await cancel_review_session(
        session,
        review_run,
        pi_agent_client=pi_agent_client,
        reason=payload.reason,
    )


@router.post(
    "/review-runs/{review_run_id}/result",
    response_model=ReviewResultCollectResponse,
)
async def collect_review_result_endpoint(
    review_run_id: str,
    payload: ReviewResultCollect,
    session: AsyncSession = session_dependency,
) -> ReviewResultCollectResponse:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        parsed = await collect_review_result(
            session,
            review_run,
            raw_output=payload.raw_output,
            changed_files=payload.changed_files,
        )
    except ReviewRunTransitionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except ReviewResultError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.to_dict(),
        ) from exc
    return ReviewResultCollectResponse(review_run=review_run, parsed=parsed)


@router.post(
    "/review-runs/{review_run_id}/retry",
    response_model=ReviewRunRetryResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_review_run_endpoint(
    review_run_id: str,
    session: AsyncSession = session_dependency,
) -> ReviewRunRetryResult:
    try:
        result = await request_review_retry(session, review_run_id)
    except ReviewRequestRejected as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=_review_request_rejection_detail(exc),
        ) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return result


@router.post(
    "/review-runs/{review_run_id}/rerun",
    response_model=ReviewRunRerunResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rerun_review_run_endpoint(
    review_run_id: str,
    payload: ReviewRunRerunRequest,
    session: AsyncSession = session_dependency,
) -> ReviewRunRerunResult:
    try:
        result = await request_review_rerun(
            session,
            review_run_id,
            idempotency_key=str(payload.idempotency_key),
        )
    except ReviewRequestRejected as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=_review_request_rejection_detail(exc),
        ) from exc
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return result


@router.post(
    "/review-runs/{review_run_id}/cancel",
    response_model=ReviewRunActionResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_review_run_endpoint(
    review_run_id: str,
    session: AsyncSession = session_dependency,
) -> ReviewRunActionResult:
    review_run = await cancel_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return ReviewRunActionResult(
        review_run_id=review_run.id,
        status=review_run.status,
    )


@router.post("/workspaces/prepare", response_model=WorkspacePrepareResponse)
async def prepare_workspace_endpoint(
    payload: WorkspacePrepareRequest,
    request: Request,
    session: AsyncSession = session_dependency,
) -> WorkspacePrepareResponse:
    return await prepare_workspace(
        session,
        request.app.state.settings,
        payload,
        provider_registry=get_configured_provider_registry(request),
    )


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceRead)
async def get_workspace_endpoint(
    workspace_id: str,
    session: AsyncSession = session_dependency,
) -> WorkspaceRead:
    workspace = await get_workspace(session, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return workspace


@router.post("/workspaces/{workspace_id}/lease", response_model=WorkspaceLeaseRead)
async def lease_workspace_endpoint(
    workspace_id: str,
    payload: WorkspaceLeaseRequest,
    session: AsyncSession = session_dependency,
) -> WorkspaceLeaseRead:
    try:
        lease, workspace = await lease_workspace(
            session,
            workspace_id,
            review_run_id=payload.review_run_id,
            session_id=payload.session_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    return WorkspaceLeaseRead(
        lease_id=lease.id,
        workspace_id=workspace.workspace_id,
        workspace_path=workspace.workspace_path,
        status=workspace.status,
    )


@router.post("/workspace-leases/{lease_id}/release", response_model=WorkspaceRead)
async def release_workspace_endpoint(
    lease_id: str,
    session: AsyncSession = session_dependency,
) -> WorkspaceRead:
    workspace = await release_workspace(session, lease_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return workspace


@router.post("/workspaces/{workspace_id}/cleanup", response_model=WorkspaceRead)
async def cleanup_workspace_endpoint(
    workspace_id: str,
    payload: WorkspaceCleanupRequest,
    session: AsyncSession = session_dependency,
) -> WorkspaceRead:
    result = await cleanup_workspace(session, workspace_id, force=payload.force)
    workspace = await get_workspace(session, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if result == "workspace_locked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace has an active lease.",
        )
    return workspace


@router.post("/workspaces/cleanup/pr", response_model=CleanupSummary)
async def cleanup_pull_request_workspaces_endpoint(
    payload: PullRequestWorkspaceCleanupRequest,
    session: AsyncSession = session_dependency,
) -> CleanupSummary:
    return await cleanup_pull_request_workspaces(
        session,
        provider=payload.provider,
        repository=payload.repository,
        pull_request_number=payload.pull_request_number,
        force=payload.force,
    )


@router.post("/workspaces/cleanup/expired", response_model=CleanupSummary)
async def cleanup_expired_workspaces_endpoint(
    session: AsyncSession = session_dependency,
) -> CleanupSummary:
    return await cleanup_expired_workspaces(session)
