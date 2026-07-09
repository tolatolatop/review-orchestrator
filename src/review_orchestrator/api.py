from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.db import get_session
from review_orchestrator.github import (
    GitHubWebhookError,
    normalize_github_event,
    parse_json_body,
    verify_signature,
)
from review_orchestrator.openhands import OpenHandsClient
from review_orchestrator.review_results import ReviewResultError
from review_orchestrator.schemas import (
    ReviewResultCollect,
    ReviewResultCollectResponse,
    ReviewRunCreate,
    ReviewRunRead,
    ReviewSessionCancel,
    ReviewSessionStart,
    WebhookAccepted,
)
from review_orchestrator.services import (
    ReviewRunTransitionError,
    accept_github_webhook,
    cancel_review_session,
    collect_review_result,
    create_review_run,
    get_review_run,
    start_review_session,
    sync_review_session,
)

router = APIRouter(prefix="/api/v1")
session_dependency = Depends(get_session)


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
        api_key=settings.openhands_api_key,
        timeout=settings.openhands_timeout_seconds,
    )


openhands_client_dependency = Depends(get_openhands_client)


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
        normalized_event = normalize_github_event(provider_event, payload)
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
