from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.models import Finding, ReviewCommentRef, ReviewRun

SUMMARY_MARKER = "<!-- review-orchestrator:summary"


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
    lines = [
        f"{SUMMARY_MARKER} "
        f"run_id={review_run.id} head_sha={review_run.head_sha} -->",
        f"Review status: {status_text}",
    ]
    if review_run.failure_code:
        lines.append(f"Failure category: {review_run.failure_code}")
    if review_run.error:
        lines.append(f"Error: {_safe_error_text(review_run.error)}")
    lines.extend(["", f"Findings: {stat_text}"])
    return "\n".join(lines)


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


async def _existing_summary_ref_with_body(
    session: AsyncSession,
    review_run: ReviewRun,
    body: str,
) -> ReviewCommentRef | None:
    result = await session.execute(
        select(ReviewCommentRef).where(
            ReviewCommentRef.provider == review_run.provider,
            ReviewCommentRef.repo_full_name == review_run.repo_full_name,
            ReviewCommentRef.pull_request_number == review_run.pull_request_number,
            ReviewCommentRef.comment_type == "summary",
            ReviewCommentRef.last_published_body_hash == body_hash(body),
        )
    )
    return result.scalar_one_or_none()


def _safe_error_text(error: str, *, max_length: int = 500) -> str:
    redacted = error.replace("\r", " ").replace("\n", " ")
    for marker in ("token", "secret", "password", "authorization"):
        redacted = _redact_marker_value(redacted, marker)
    if len(redacted) > max_length:
        return redacted[: max_length - 3].rstrip() + "..."
    return redacted


def _redact_marker_value(text: str, marker: str) -> str:
    lowered = text.lower()
    start = 0
    while True:
        index = lowered.find(marker, start)
        if index == -1:
            return text
        value_start = index + len(marker)
        while value_start < len(text) and text[value_start] in " \t:=_-":
            value_start += 1
        value_end = value_start
        while value_end < len(text) and text[value_end] not in " \t,;":
            value_end += 1
        if value_end > value_start:
            text = text[:value_start] + "[redacted]" + text[value_end:]
            lowered = text.lower()
            start = value_start + len("[redacted]")
        else:
            start = value_start


async def _existing_line_ref(
    session: AsyncSession,
    finding: Finding,
) -> ReviewCommentRef | None:
    result = await session.execute(
        select(ReviewCommentRef).where(
            ReviewCommentRef.finding_id == finding.id,
            ReviewCommentRef.comment_type == "line",
        )
    )
    return result.scalar_one_or_none()


def _line_comment_body(finding: Finding) -> str:
    body = f"{finding.message}\n\nSeverity: {finding.severity}"
    if finding.suggestion:
        body += f"\n\nSuggestion: {finding.suggestion}"
    return body
