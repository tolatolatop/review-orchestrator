from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.models import Finding, ReviewCommentRef, ReviewRun


def build_summary_comment_body(
    review_run: ReviewRun,
    *,
    status_text: str,
    finding_stats: dict[str, int] | None = None,
) -> str:
    stats = finding_stats or {}
    stat_text = ", ".join(f"{key}: {value}" for key, value in sorted(stats.items()))
    if not stat_text:
        stat_text = "no findings published"
    return (
        "<!-- review-orchestrator:summary "
        f"run_id={review_run.id} head_sha={review_run.head_sha} -->\n"
        f"Review status: {status_text}\n\n"
        f"Findings: {stat_text}"
    )


async def upsert_summary_comment_ref(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    provider_comment_id: str,
    body: str,
) -> ReviewCommentRef:
    result = await session.execute(
        select(ReviewCommentRef).where(
            ReviewCommentRef.provider == review_run.provider,
            ReviewCommentRef.repo_full_name == review_run.repo_full_name,
            ReviewCommentRef.pull_request_number == review_run.pull_request_number,
            ReviewCommentRef.comment_type == "summary",
        )
    )
    comment_ref = result.scalar_one_or_none()
    if comment_ref is None:
        comment_ref = ReviewCommentRef(
            provider=review_run.provider,
            repo_full_name=review_run.repo_full_name,
            pull_request_number=review_run.pull_request_number,
            review_run_id=review_run.id,
            comment_type="summary",
            provider_comment_id=provider_comment_id,
        )
    comment_ref.review_run_id = review_run.id
    comment_ref.provider_comment_id = provider_comment_id
    comment_ref.status = "active"
    comment_ref.last_published_body_hash = body_hash(body)
    review_run.summary_comment_id = provider_comment_id
    session.add(comment_ref)
    session.add(review_run)
    await session.commit()
    await session.refresh(comment_ref)
    return comment_ref


async def ensure_line_comment_ref(
    session: AsyncSession,
    review_run: ReviewRun,
    finding: Finding,
    *,
    provider_comment_id: str,
    body: str,
) -> tuple[ReviewCommentRef, bool]:
    result = await session.execute(
        select(ReviewCommentRef).where(
            ReviewCommentRef.finding_id == finding.id,
            ReviewCommentRef.comment_type == "line",
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing, False

    comment_ref = ReviewCommentRef(
        provider=review_run.provider,
        repo_full_name=review_run.repo_full_name,
        pull_request_number=review_run.pull_request_number,
        review_run_id=review_run.id,
        finding_id=finding.id,
        comment_type="line",
        provider_comment_id=provider_comment_id,
        status="active",
        last_published_body_hash=body_hash(body),
    )
    session.add(comment_ref)
    await session.commit()
    await session.refresh(comment_ref)
    return comment_ref, True


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
