from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.db import get_session
from review_orchestrator.github import GitHubAdapter
from review_orchestrator.gitlab import GitLabAdapter
from review_orchestrator.openhands import OpenHandsClient
from review_orchestrator.providers import ProviderRegistry, ProviderWebhookError
from review_orchestrator.review_results import ReviewResultError
from review_orchestrator.schemas import (
    CleanupSummary,
    OpenHandsSessionDiagnostics,
    ProviderEventInboxDetail,
    ProviderEventInboxListResponse,
    PullRequestWorkspaceCleanupRequest,
    ReviewResultCollect,
    ReviewResultCollectResponse,
    ReviewRunActionResult,
    ReviewRunCreate,
    ReviewRunRead,
    ReviewSessionCancel,
    ReviewSessionStart,
    WebhookAccepted,
    WorkspaceCleanupRequest,
    WorkspaceLeaseRead,
    WorkspaceLeaseRequest,
    WorkspacePrepareRequest,
    WorkspacePrepareResponse,
    WorkspaceRead,
)
from review_orchestrator.services import (
    ReviewRunTransitionError,
    accept_provider_webhook,
    cancel_review_run,
    cancel_review_session,
    collect_review_result,
    create_review_run,
    get_openhands_session_diagnostics_for_conversation,
    get_openhands_session_diagnostics_for_review_run,
    get_provider_event_inbox_detail,
    get_review_run,
    list_provider_event_inbox,
    retry_review_run,
    start_review_session,
    sync_review_session,
)
from review_orchestrator.workspaces import (
    cleanup_expired_workspaces,
    cleanup_pull_request_workspaces,
    cleanup_workspace,
    get_workspace,
    lease_workspace,
    prepare_workspace,
    release_workspace,
)

router = APIRouter(prefix="/api/v1")
session_dependency = Depends(get_session)
provider_registry = ProviderRegistry([GitHubAdapter(), GitLabAdapter()])


def get_openhands_client(request: Request) -> OpenHandsClient:
    injected = getattr(request.app.state, "openhands_client", None)
    if injected is not None:
        return injected

    settings = request.app.state.settings
    if not settings.openhands_base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OpenHands base URL is not configured.",
        )
    return OpenHandsClient(
        base_url=settings.openhands_base_url,
        api_key=settings.openhands_api_token,
        timeout=settings.openhands_timeout_seconds,
    )


openhands_client_dependency = Depends(get_openhands_client)


def get_optional_openhands_client(
    request: Request,
) -> tuple[OpenHandsClient | None, str | None]:
    injected = getattr(request.app.state, "openhands_client", None)
    if injected is not None:
        return injected, None

    settings = request.app.state.settings
    if not settings.openhands_base_url:
        return None, "OpenHands base URL is not configured."
    return (
        OpenHandsClient(
            base_url=settings.openhands_base_url,
            api_key=settings.openhands_api_token,
            timeout=settings.openhands_timeout_seconds,
        ),
        None,
    )


@router.post("/webhooks/{provider}", response_model=WebhookAccepted)
async def accept_webhook(
    provider: str,
    request: Request,
    session: AsyncSession = session_dependency,
) -> WebhookAccepted:
    adapter = provider_registry.get(provider)
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


@router.post(
    "/review-runs",
    response_model=ReviewRunRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_review_run_endpoint(
    payload: ReviewRunCreate,
    session: AsyncSession = session_dependency,
) -> ReviewRunRead:
    return await create_review_run(session, payload)


@router.get("/review-runs/{review_run_id}", response_model=ReviewRunRead)
async def get_review_run_endpoint(
    review_run_id: str,
    session: AsyncSession = session_dependency,
) -> ReviewRunRead:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return review_run


@router.get(
    "/observability/review-runs/{review_run_id}/openhands-session",
    response_model=OpenHandsSessionDiagnostics,
)
async def get_review_run_openhands_session_endpoint(
    review_run_id: str,
    request: Request,
    session: AsyncSession = session_dependency,
) -> OpenHandsSessionDiagnostics:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    openhands_client, disabled_reason = get_optional_openhands_client(request)
    return await get_openhands_session_diagnostics_for_review_run(
        session,
        review_run,
        openhands_client=openhands_client,
        openhands_live_status_disabled_reason=disabled_reason,
        openhands_ui_base_url=request.app.state.settings.openhands_ui_base_url,
    )


@router.get(
    "/observability/openhands-sessions/{conversation_id}",
    response_model=OpenHandsSessionDiagnostics,
)
async def get_openhands_session_endpoint(
    conversation_id: str,
    request: Request,
    session: AsyncSession = session_dependency,
) -> OpenHandsSessionDiagnostics:
    openhands_client, disabled_reason = get_optional_openhands_client(request)
    diagnostics = await get_openhands_session_diagnostics_for_conversation(
        session,
        conversation_id,
        openhands_client=openhands_client,
        openhands_live_status_disabled_reason=disabled_reason,
        openhands_ui_base_url=request.app.state.settings.openhands_ui_base_url,
    )
    if diagnostics is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return diagnostics


@router.post("/review-runs/{review_run_id}/session/start", response_model=ReviewRunRead)
async def start_review_session_endpoint(
    review_run_id: str,
    payload: ReviewSessionStart,
    session: AsyncSession = session_dependency,
    openhands_client: OpenHandsClient = openhands_client_dependency,
) -> ReviewRunRead:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        return await start_review_session(
            session,
            review_run,
            openhands_client=openhands_client,
            workspace_path=payload.workspace_path,
        )
    except ReviewRunTransitionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/review-runs/{review_run_id}/session/sync", response_model=ReviewRunRead)
async def sync_review_session_endpoint(
    review_run_id: str,
    session: AsyncSession = session_dependency,
    openhands_client: OpenHandsClient = openhands_client_dependency,
) -> ReviewRunRead:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return await sync_review_session(
        session,
        review_run,
        openhands_client=openhands_client,
    )


@router.post(
    "/review-runs/{review_run_id}/session/cancel",
    response_model=ReviewRunRead,
)
async def cancel_review_session_endpoint(
    review_run_id: str,
    payload: ReviewSessionCancel,
    session: AsyncSession = session_dependency,
    openhands_client: OpenHandsClient = openhands_client_dependency,
) -> ReviewRunRead:
    review_run = await get_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return await cancel_review_session(
        session,
        review_run,
        openhands_client=openhands_client,
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
    response_model=ReviewRunActionResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_review_run_endpoint(
    review_run_id: str,
    session: AsyncSession = session_dependency,
) -> ReviewRunActionResult:
    review_run = await retry_review_run(session, review_run_id)
    if review_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if review_run.id == review_run_id and review_run.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only failed review runs can be retried without force.",
        )
    return ReviewRunActionResult(
        review_run_id=review_run.id,
        status=review_run.status,
    )


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
    return await prepare_workspace(session, request.app.state.settings, payload)


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
