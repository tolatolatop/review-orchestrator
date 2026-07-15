"""Test-only database factories that do not expose production use cases."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.domain.models import ReviewRun


@dataclass(frozen=True)
class ReviewRunSeed:
    provider: str
    repo_full_name: str
    pull_request_number: int
    head_sha: str
    base_sha: str | None = None
    force: bool = False


async def seed_review_run(
    session: AsyncSession,
    payload: ReviewRunSeed,
    *,
    trigger_type: str = "test",
    trigger_event_id: str | None = None,
) -> ReviewRun:
    result = await session.execute(
        select(ReviewRun)
        .where(
            ReviewRun.provider == payload.provider,
            ReviewRun.repo_full_name == payload.repo_full_name,
            ReviewRun.pull_request_number == payload.pull_request_number,
            ReviewRun.head_sha == payload.head_sha,
        )
        .order_by(ReviewRun.attempt.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is not None and not payload.force:
        return latest
    attempt = 1 if latest is None else latest.attempt + 1
    review_run = ReviewRun(
        provider=payload.provider,
        repo_full_name=payload.repo_full_name,
        pull_request_number=payload.pull_request_number,
        base_sha=payload.base_sha,
        head_sha=payload.head_sha,
        attempt=attempt,
        capability_id="code-review",
        status="queued",
        execution_status="pending",
        delivery_status="pending",
        queue="manual-review",
        priority=60,
        effective_priority=60,
        trigger_type=trigger_type,
        trigger_event_id=trigger_event_id,
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
    )
    session.add(review_run)
    await session.commit()
    await session.refresh(review_run)
    return review_run


# Keep existing test call sites compact while production no longer exposes these names.
ReviewRunCreate = ReviewRunSeed
create_review_run = seed_review_run
