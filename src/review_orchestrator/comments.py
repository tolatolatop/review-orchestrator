from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.github import GitHubClient, GitHubClientError
from review_orchestrator.gitlab import GitLabClient, GitLabClientError
from review_orchestrator.models import Finding, ReviewCommentRef, ReviewRun
from review_orchestrator.review_results import ChangedFile

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


async def publish_github_summary_comment(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    github_client: GitHubClient,
    status_text: str,
    finding_stats: dict[str, int] | None = None,
) -> ReviewCommentRef | None:
    body = build_summary_comment_body(
        review_run,
        status_text=status_text,
        finding_stats=finding_stats,
    )
    existing_ref = await _existing_summary_ref_with_body(session, review_run, body)
    if existing_ref is not None:
        return existing_ref
    try:
        provider_comment_id = await _upsert_github_issue_comment(
            github_client,
            review_run,
            body,
        )
    except GitHubClientError as exc:
        review_run.failure_code = "provider_permission_denied"
        review_run.error = str(exc)
        session.add(review_run)
        await session.commit()
        return None
    return await upsert_summary_comment_ref(
        session,
        review_run,
        provider_comment_id=provider_comment_id,
        body=body,
    )


async def publish_github_line_comments(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    github_client: GitHubClient,
    changed_files: list[ChangedFile],
) -> dict[str, int]:
    commentable = {item.path: item.commentable_lines for item in changed_files}
    result = await session.execute(
        select(Finding).where(
            Finding.review_run_id == review_run.id,
            Finding.status == "active",
        )
    )
    stats = {"published": 0, "summary_only": 0, "deduped": 0, "failed": 0}
    for finding in result.scalars().all():
        line = finding.line_start
        file_lines = commentable.get(finding.file_path)
        if (
            line is None
            or file_lines is None
            or not file_lines
            or line not in file_lines
        ):
            stats["summary_only"] += 1
            continue

        body = _line_comment_body(finding)
        existing = await _existing_line_ref(session, finding)
        if existing is not None:
            stats["deduped"] += 1
            continue
        try:
            provider_comment_id = await github_client.create_review_comment(
                review_run.repo_full_name,
                review_run.pull_request_number,
                body=body,
                commit_id=review_run.head_sha,
                path=finding.file_path,
                line=line,
            )
        except GitHubClientError as exc:
            review_run.failure_code = "provider_permission_denied"
            review_run.error = str(exc)
            session.add(review_run)
            await session.commit()
            stats["failed"] += 1
            continue
        _, created = await ensure_line_comment_ref(
            session,
            review_run,
            finding,
            provider_comment_id=provider_comment_id,
            body=body,
        )
        stats["published" if created else "deduped"] += 1
    return stats


async def publish_gitlab_summary_comment(
    session: AsyncSession,
    review_run: ReviewRun,
    *,
    gitlab_client: GitLabClient,
    status_text: str,
    finding_stats: dict[str, int] | None = None,
) -> ReviewCommentRef | None:
    body = build_summary_comment_body(
        review_run,
        status_text=status_text,
        finding_stats=finding_stats,
    )
    existing_ref = await _existing_summary_ref_with_body(session, review_run, body)
    if existing_ref is not None:
        return existing_ref
    try:
        provider_comment_id = await _upsert_gitlab_note(
            gitlab_client,
            review_run,
            body,
        )
    except GitLabClientError as exc:
        review_run.failure_code = "provider_permission_denied"
        review_run.error = str(exc)
        session.add(review_run)
        await session.commit()
        return None
    return await upsert_summary_comment_ref(
        session,
        review_run,
        provider_comment_id=provider_comment_id,
        body=body,
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


async def _upsert_github_issue_comment(
    github_client: GitHubClient,
    review_run: ReviewRun,
    body: str,
) -> str:
    if review_run.summary_comment_id:
        return await github_client.update_issue_comment(
            review_run.repo_full_name,
            review_run.summary_comment_id,
            body,
        )

    for comment in await github_client.list_issue_comments(
        review_run.repo_full_name,
        review_run.pull_request_number,
    ):
        if comment.body and SUMMARY_MARKER in comment.body:
            return await github_client.update_issue_comment(
                review_run.repo_full_name,
                str(comment.id),
                body,
            )
    return await github_client.create_issue_comment(
        review_run.repo_full_name,
        review_run.pull_request_number,
        body,
    )


async def _upsert_gitlab_note(
    gitlab_client: GitLabClient,
    review_run: ReviewRun,
    body: str,
) -> str:
    if review_run.summary_comment_id:
        return await gitlab_client.update_merge_request_note(
            review_run.repo_full_name,
            review_run.pull_request_number,
            review_run.summary_comment_id,
            body,
        )

    for note in await gitlab_client.list_merge_request_notes(
        review_run.repo_full_name,
        review_run.pull_request_number,
    ):
        if note.body and SUMMARY_MARKER in note.body:
            return await gitlab_client.update_merge_request_note(
                review_run.repo_full_name,
                review_run.pull_request_number,
                str(note.id),
                body,
            )
    return await gitlab_client.create_merge_request_note(
        review_run.repo_full_name,
        review_run.pull_request_number,
        body,
    )


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
