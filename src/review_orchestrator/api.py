from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.db import get_session
from review_orchestrator.schemas import ReviewRunCreate, ReviewRunRead, WebhookAccepted
from review_orchestrator.services import create_review_run, get_review_run

router = APIRouter(prefix="/api/v1")
session_dependency = Depends(get_session)


@router.post("/webhooks/{provider}", response_model=WebhookAccepted)
async def accept_webhook(provider: str, payload: dict[str, Any]) -> WebhookAccepted:
    # MVP placeholder: validate/authenticate provider payloads before enqueueing work.
    _ = payload
    return WebhookAccepted(provider=provider)


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
