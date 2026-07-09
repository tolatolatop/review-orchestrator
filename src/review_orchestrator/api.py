from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.db import get_session
from review_orchestrator.github import (
    GitHubWebhookError,
    normalize_github_event,
    parse_json_body,
    verify_signature,
)
from review_orchestrator.schemas import (
    CleanupSummary,
    PullRequestWorkspaceCleanupRequest,
    ReviewRunActionResult,
    ReviewRunCreate,
    ReviewRunRead,
    WebhookAccepted,
    WorkspaceCleanupRequest,
    WorkspaceLeaseRead,
    WorkspaceLeaseRequest,
    WorkspacePrepareRequest,
    WorkspacePrepareResponse,
    WorkspaceRead,
)
from review_orchestrator.services import (
    accept_github_webhook,
    cancel_review_run,
    create_review_run,
    get_review_run,
    retry_review_run,
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


@router.post("/webhooks/{provider}", response_model=WebhookAccepted)
async def accept_webhook(
    provider: str,
    request: Request,
    session: AsyncSession = session_dependency,
) -> WebhookAccepted:
    if provider != "github":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unsupported provider: {provider}",
        )

    raw_body = await request.body()
    delivery_id = request.headers.get("X-GitHub-Delivery")
    provider_event = request.headers.get("X-GitHub-Event")
    if not delivery_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-GitHub-Delivery header.",
        )
    if not provider_event:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-GitHub-Event header.",
        )

    try:
        verify_signature(
            raw_body,
            request.headers.get("X-Hub-Signature-256"),
            request.app.state.settings.github_webhook_secret,
        )
        payload = parse_json_body(raw_body)
        normalized_event = normalize_github_event(
            provider_event,
            payload,
            bot_login=request.app.state.settings.review_bot_login,
        )
    except GitHubWebhookError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return await accept_github_webhook(
        session,
        delivery_id=delivery_id,
        provider_event=provider_event,
        normalized_event=normalized_event,
        payload=payload,
        raw_body=raw_body,
    )


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
