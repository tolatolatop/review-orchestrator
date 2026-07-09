from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.models import ReviewRun
from review_orchestrator.schemas import ReviewRunCreate


async def create_review_run(
    session: AsyncSession,
    payload: ReviewRunCreate,
) -> ReviewRun:
    existing = await get_review_run_by_head(
        session,
        provider=payload.provider,
        repository=payload.repository,
        pull_request_number=payload.pull_request_number,
        head_sha=payload.head_sha,
    )
    if existing:
        return existing

    review_run = ReviewRun(**payload.model_dump(), status="queued")
    session.add(review_run)
    await session.commit()
    await session.refresh(review_run)
    return review_run


async def get_review_run(
    session: AsyncSession,
    review_run_id: str,
) -> ReviewRun | None:
    return await session.get(ReviewRun, review_run_id)


async def get_review_run_by_head(
    session: AsyncSession,
    *,
    provider: str,
    repository: str,
    pull_request_number: int,
    head_sha: str,
) -> ReviewRun | None:
    result = await session.execute(
        select(ReviewRun).where(
            ReviewRun.provider == provider,
            ReviewRun.repository == repository,
            ReviewRun.pull_request_number == pull_request_number,
            ReviewRun.head_sha == head_sha,
        )
    )
    return result.scalar_one_or_none()
