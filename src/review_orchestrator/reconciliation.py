from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from review_orchestrator.models import Finding, ReviewRun
from review_orchestrator.review_results import ParsedReviewResult


@dataclass(frozen=True)
class ReconciliationStats:
    new: int = 0
    existing: int = 0
    resolved: int = 0
    stale: int = 0


async def persist_and_reconcile_findings(
    session: AsyncSession,
    review_run: ReviewRun,
    parsed_result: ParsedReviewResult,
) -> ReconciliationStats:
    previous_findings = await _previous_active_findings(session, review_run)
    previous_by_fingerprint = {
        finding.fingerprint: finding for finding in previous_findings
    }
    seen_fingerprints: set[str] = set()
    stats = {"new": 0, "existing": 0, "resolved": 0, "stale": 0}

    for publishable in parsed_result.findings:
        finding_payload = publishable.finding
        previous = previous_by_fingerprint.get(publishable.fingerprint)
        state = "existing" if previous else "new"
        if previous:
            stats["existing"] += 1
            first_seen_run_id = previous.first_seen_run_id or previous.review_run_id
        else:
            stats["new"] += 1
            first_seen_run_id = review_run.id

        finding = Finding(
            review_run_id=review_run.id,
            pull_request_context_id=review_run.pull_request_context_id,
            fingerprint=publishable.fingerprint,
            file_path=finding_payload.file,
            line_start=finding_payload.line,
            line_end=finding_payload.line_end,
            severity=finding_payload.severity,
            category=finding_payload.category,
            message=finding_payload.message,
            suggestion=finding_payload.suggestion,
            confidence=finding_payload.confidence,
            status="active",
            state=state,
            first_seen_run_id=first_seen_run_id,
            last_seen_run_id=review_run.id,
            raw_payload_json=finding_payload.model_dump(mode="json"),
        )
        session.add(finding)
        seen_fingerprints.add(publishable.fingerprint)

    for previous in previous_findings:
        if previous.fingerprint in seen_fingerprints:
            continue
        previous.status = "resolved"
        previous.state = "resolved"
        previous.resolved_run_id = review_run.id
        stats["resolved"] += 1
        session.add(previous)

    review_run.finding_count_total = len(parsed_result.findings)
    review_run.finding_count_by_severity = _severity_counts(parsed_result)
    session.add(review_run)
    await session.commit()
    return ReconciliationStats(**stats)


async def archive_findings_for_merged_pr(
    session: AsyncSession,
    review_run: ReviewRun,
) -> dict[str, int]:
    result = await session.execute(
        select(Finding).where(
            Finding.review_run_id == review_run.id,
            Finding.status == "active",
        )
    )
    active_findings = list(result.scalars().all())
    stats = {"accepted": 0, "rejected": 0, "stale": 0}
    for finding in active_findings:
        if finding.state == "stale":
            finding.outcome = "stale"
            stats["stale"] += 1
        else:
            finding.outcome = "rejected"
            stats["rejected"] += 1
        session.add(finding)
    await session.commit()
    return stats


async def _previous_active_findings(
    session: AsyncSession,
    review_run: ReviewRun,
) -> list[Finding]:
    result = await session.execute(
        select(ReviewRun)
        .where(
            ReviewRun.provider == review_run.provider,
            ReviewRun.repo_full_name == review_run.repo_full_name,
            ReviewRun.pull_request_number == review_run.pull_request_number,
            ReviewRun.id != review_run.id,
            ReviewRun.status == "completed",
        )
        .order_by(
            ReviewRun.completed_at.desc().nullslast(),
            ReviewRun.created_at.desc(),
        )
        .limit(1)
    )
    previous_run = result.scalar_one_or_none()
    if previous_run is None:
        return []

    findings = await session.execute(
        select(Finding).where(
            Finding.review_run_id == previous_run.id,
            Finding.status == "active",
        )
    )
    return list(findings.scalars().all())


def _severity_counts(parsed_result: ParsedReviewResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for publishable in parsed_result.findings:
        severity = str(publishable.finding.severity)
        counts[severity] = counts.get(severity, 0) + 1
    return counts
